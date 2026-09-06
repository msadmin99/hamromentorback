from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class GlobalSafeListPagination(PageNumberPagination):
    """The DRF-wide default (see REST_FRAMEWORK['DEFAULT_PAGINATION_CLASS']
    in settings.py) — a backstop for any ModelViewSet/ListAPIView that
    doesn't already set its own `pagination_class`, so no endpoint added in
    the future silently becomes another unbounded-growth case.

    Every ViewSet reviewed and fixed under the scalability audit
    (Question/Test/Subject/Chapter/Topic/Purchase/Enrollment) already sets
    its own `pagination_class`, which takes precedence over this default —
    this class only ever applies to a view that hasn't been given one.

    Deliberately preserves the existing bare-array response shape (like
    every per-app `_BoundedListPagination` this mirrors) rather than DRF's
    default `{count, next, previous, results}` envelope: applying the
    enveloped shape globally would be a breaking response-shape change for
    every untouched endpoint's existing frontend caller, which the "do not
    change existing UI/UX" rule forbids. A view that genuinely needs the
    enveloped, paginated UX (browsing a whole catalog) opts in explicitly
    with its own pagination_class instead, as `browse` actions already do.
    """
    page_size = 500
    max_page_size = 500

    def get_paginated_response(self, data):
        return Response(data)
