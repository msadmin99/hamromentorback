import pymysql

# PyMySQL is pure Python (no C compiler needed), unlike mysqlclient — more
# reliable to install on shared cPanel hosting. This makes Django use it as
# a drop-in replacement wherever it expects the MySQLdb driver.
pymysql.install_as_MySQLdb()
