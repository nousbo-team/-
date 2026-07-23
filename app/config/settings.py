"""
Django settings for config project (누보 포장지 발주관리 시스템 프로토타입).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'dev-insecure-local-prototype-key')

DEBUG = os.environ.get('DJANGO_DEBUG', 'True') == 'True'

ALLOWED_HOSTS = [h.strip() for h in os.environ.get('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost,testserver').split(',') if h.strip()]
# Render는 배포된 서비스의 도메인을 이 환경변수로 자동 주입한다.
_render_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
if _render_host:
    ALLOWED_HOSTS.append(_render_host)

CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()]
if _render_host:
    CSRF_TRUSTED_ORIGINS.append(f'https://{_render_host}')

# Render는 TLS를 프록시에서 종료하고 X-Forwarded-Proto로 전달한다.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# 인터넷에 공개되는 배포(DEBUG=False)에서는 쿠키/리다이렉트를 HTTPS 전용으로 강제한다.
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'accounts',
    'catalog',
    'workflow',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'workflow.context_processors.unread_notifications',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# 프로토타입(로컬): SQLite — 설치 없이 즉시 구동.
# 클라우드(Render+Supabase): .env의 DATABASE_URL 하나만 채우면 된다(Supabase 프로젝트의
# "Connection string" 그대로). 코드/모델 변경 불필요 — Django ORM이 추상화한다.
_database_url = os.environ.get('DATABASE_URL')
if _database_url:
    import dj_database_url
    DATABASES = {'default': dj_database_url.config(default=_database_url, conn_max_age=600)}
elif os.environ.get('DB_ENGINE') == 'postgresql':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ.get('DB_NAME', 'packaging_mgmt'),
            'USER': os.environ.get('DB_USER', 'postgres'),
            'PASSWORD': os.environ.get('DB_PASSWORD', ''),
            'HOST': os.environ.get('DB_HOST', 'localhost'),
            'PORT': os.environ.get('DB_PORT', '5432'),
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


LANGUAGE_CODE = 'ko-kr'
TIME_ZONE = 'Asia/Seoul'
USE_I18N = True
USE_TZ = True


STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}

# 업로드 파일(포장지 AI/JPG) 저장 위치.
# 로컬: 파일시스템. 클라우드(Render): 웹서비스에 디스크가 없어 재배포 시 파일이
# 사라지므로, Supabase Storage(S3 호환)의 AWS_* 값을 .env에 채우면 자동으로 그쪽에
# 저장된다 — 모델/뷰 코드 변경 없음.
_aws_bucket = os.environ.get('AWS_STORAGE_BUCKET_NAME')
if _aws_bucket:
    STORAGES['default'] = {'BACKEND': 'storages.backends.s3.S3Storage'}
    AWS_STORAGE_BUCKET_NAME = _aws_bucket
    AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID', '')
    AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY', '')
    AWS_S3_ENDPOINT_URL = os.environ.get('AWS_S3_ENDPOINT_URL', '')
    AWS_S3_REGION_NAME = os.environ.get('AWS_S3_REGION_NAME', 'ap-northeast-2')
    AWS_DEFAULT_ACL = None
    AWS_QUERYSTRING_AUTH = True
    # 버킷명에 점(.)이 있거나(nousbo.team) Supabase 같은 S3 호환 스토리지에서는
    # virtual-hosted-style(버킷명.엔드포인트) 대신 path-style(엔드포인트/버킷명)을
    # 써야 서명(Signature)이 맞는다 — 안 하면 SignatureDoesNotMatch 에러가 난다.
    AWS_S3_ADDRESSING_STYLE = 'path'
    AWS_S3_SIGNATURE_VERSION = 's3v4'
    MEDIA_URL = os.environ.get('MEDIA_URL_OVERRIDE', '/media/')
else:
    STORAGES['default'] = {'BACKEND': 'django.core.files.storage.FileSystemStorage'}
    MEDIA_URL = '/media/'
    MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'accounts:login'
LOGIN_REDIRECT_URL = 'workflow:dashboard'
LOGOUT_REDIRECT_URL = 'accounts:login'

# 이메일 — .env에 EMAIL_HOST를 채우면 실제 SMTP로 발송(예: 각자 아이디@nousbo.com).
# 비워두면 콘솔/로그 출력으로만 남는 모의(mock) 발송으로 자동 대체된다.
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
if EMAIL_HOST:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
    EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
    EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
    EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True') == 'True'
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'noreply@nousbo.com')

# 카카오톡/문자는 발신 API 계약(알림톡 등)이 아직 없어 실제 발송 대신 로그로만 남긴다
# (PRD Open Question). 이메일은 위 EMAIL_HOST 설정 여부로 실제/모의가 자동 전환된다.
MOCK_KAKAO_NOTIFICATIONS = True

LOGS_DIR = BASE_DIR / 'logs'
LOGS_DIR.mkdir(exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'simple': {'format': '%(asctime)s %(levelname)s %(name)s: %(message)s'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'simple'},
        'notify_file': {
            'class': 'logging.FileHandler',
            'filename': LOGS_DIR / 'notifications.log',
            'formatter': 'simple',
            'encoding': 'utf-8',
        },
    },
    'loggers': {
        'workflow.notify': {
            'handlers': ['console', 'notify_file'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}
