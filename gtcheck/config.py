from os.path import abspath, dirname, join

APP_ROOT = dirname(abspath(__file__))
URL = '127.0.0.1'
PORT = '5000'
LOG_DIR = join(APP_ROOT, 'logs')
DATA_DIR = join(APP_ROOT, 'data')
SYMLINK_DIR = join(APP_ROOT, 'static/symlink')