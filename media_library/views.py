import json

from django.conf import settings
from django.core.exceptions import ValidationError
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import MediaAsset
from .permissions_util import allowed_image_types_for, get_owner_role
from .serializers import MediaAssetSerializer
from .service import create_media_asset_from_file, delete_media_asset


class MediaUploadView(APIView):
    """
    POST /api/media/upload/  (multipart: file, image_type, category?, question_id?, option_id?)

    Validates the upload against the caller's role, stores the original in
    private GCS, creates a MediaAsset, and enqueues async variant
    generation. Returns 202 with the MediaAsset (status=processing) —
    poll GET /api/media/<id>/ for readiness.

    If an identical file (by content hash) was already processed for this
    image_type, the existing processed variants are reused immediately
    (status=ready in the response, no processing wait) — this is the
    dedup path: same bytes are never re-uploaded or re-processed twice.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request):
        image_type = request.data.get('image_type')
        category = request.data.get('category', 'other')
        file_obj = request.FILES.get('file')

        if not file_obj:
            return Response({'detail': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)

        owner_role = get_owner_role(request.user)
        allowed = allowed_image_types_for(owner_role)
        if image_type not in allowed:
            return Response(
                {'detail': f'Role "{owner_role}" is not permitted to upload image_type "{image_type}".'},
                status=status.HTTP_403_FORBIDDEN,
            )

        question_id = request.data.get('question_id') or None
        option_id = request.data.get('option_id') or None

        try:
            asset = create_media_asset_from_file(
                file_obj, image_type, owner=request.user, owner_role=owner_role, category=category,
                question_id=question_id, option_id=option_id, original_filename=file_obj.name,
            )
        except ValidationError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        response_status = status.HTTP_201_CREATED if asset.processing_status == 'ready' else status.HTTP_202_ACCEPTED
        return Response(MediaAssetSerializer(asset).data, status=response_status)


class MediaAssetDetailView(APIView):
    """GET /api/media/<uuid>/ — poll for processing status/URLs.
    DELETE /api/media/<uuid>/ — permanent delete (dedup-aware GCS cleanup,
    audit-logged). Staff-only: an asset may still be referenced by a
    Question/Option another admin owns, so this isn't opened up to any
    authenticated uploader — only staff, matching this app's existing
    IsAdminUser convention for destructive actions."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            asset = MediaAsset.objects.get(id=pk)
        except MediaAsset.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(MediaAssetSerializer(asset).data)

    def delete(self, request, pk):
        from core.deletion_audit import record_deletion

        if not (request.user.is_authenticated and request.user.is_staff):
            return Response({'detail': 'Staff only.'}, status=status.HTTP_403_FORBIDDEN)

        try:
            asset = MediaAsset.objects.get(id=pk)
        except MediaAsset.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        label = f'{asset.image_type} ({asset.original_filename})'
        try:
            delete_media_asset(asset)
        except Exception as exc:
            record_deletion(request, 'MediaAsset', pk, label, result='failure', failure_reason=str(exc)[:500])
            return Response({'detail': 'Deletion failed. No partial deletion should remain.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        record_deletion(request, 'MediaAsset', pk, label, result='success')
        return Response({'detail': 'Deletion successful. The selected data has been permanently removed from the live system.'})


class MediaProcessingHandlerView(APIView):
    """
    POST /api/media/process/ — internal endpoint invoked by Cloud Tasks
    (or directly, synchronously, when IMAGE_PROCESSING_ASYNC=False). Auth
    is a shared secret header, matching this project's existing
    X-Cron-Secret convention for other internal/cron endpoints.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        provided = request.headers.get('X-Media-Processing-Secret')
        if provided != settings.MEDIA_PROCESSING_SECRET:
            return Response({'detail': 'Invalid secret.'}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            body = json.loads(request.body)
            media_asset_id = body['media_asset_id']
        except (json.JSONDecodeError, KeyError):
            return Response({'detail': 'media_asset_id required.'}, status=status.HTTP_400_BAD_REQUEST)

        from .tasks import process_media_asset
        process_media_asset(media_asset_id)
        return Response({'ok': True})
