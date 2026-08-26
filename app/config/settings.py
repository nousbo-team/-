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
                'workflow.context_processors.web_push',
                'workflow.context_processors.ai_assistant',
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

# 로컬 개발 DB(SQLite)를 운영(Supabase) 최신 데이터로 맞추는 sync_from_prod 명령 전용 —
# 'default'는 항상 로컬 그대로 두고, 운영은 이 별도 alias로만 "읽어서 로컬로 복사"한다
# (로컬 실수로 운영에 쓰기가 되는 사고를 원천 차단).
_prod_database_url = os.environ.get('PROD_DATABASE_URL')
if _prod_database_url:
    import dj_database_url as _dj_database_url
    DATABASES['prod'] = _dj_database_url.parse(_prod_database_url, conn_max_age=0)


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
# 해시 매니페스트 스토리지는 collectstatic을 거쳐야 동작한다(Render 빌드 시 자동 실행).
# 로컬 개발/테스트(runserver, manage.py test)에서는 collectstatic 없이도 바로
# 동작하도록 DEBUG일 때만 일반 스토리지로 폴백한다.
STORAGES = {
    'staticfiles': {
        'BACKEND': (
            'django.contrib.staticfiles.storage.StaticFilesStorage' if DEBUG
            else 'whitenoise.storage.CompressedManifestStaticFilesStorage'
        ),
    },
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

    # AWS_S3_CLIENT_CONFIG가 설정되면 django-storages는 addressing_style/
    # signature_version을 별도 설정에서 읽지 않고 이 Config 하나만 사용하므로
    # 여기에 전부 몰아넣는다.
    #  - addressing_style='path': 버킷명에 점(.)이 있거나(nousbo.team) Supabase 같은
    #    S3 호환 스토리지에서는 virtual-hosted-style 대신 path-style을 써야 서명이 맞는다.
    #  - request/response_checksum_calculation='when_required': boto3가 최근 기본으로
    #    켠 자동 체크섬(x-amz-checksum-*)을 Supabase Storage가 검증하지 못해
    #    SignatureDoesNotMatch로 실패하는 문제를 막는다.
    from botocore.config import Config as _BotoConfig
    AWS_S3_CLIENT_CONFIG = _BotoConfig(
        signature_version='s3v4',
        s3={'addressing_style': 'path'},
        request_checksum_calculation='when_required',
        response_checksum_validation='when_required',
    )
    MEDIA_URL = os.environ.get('MEDIA_URL_OVERRIDE', '/media/')
else:
    STORAGES['default'] = {'BACKEND': 'django.core.files.storage.FileSystemStorage'}
    MEDIA_URL = '/media/'
    MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'accounts:login'
LOGIN_REDIRECT_URL = 'workflow:dashboard'
LOGOUT_REDIRECT_URL = 'accounts:login'

# 이메일 — BREVO_API_KEY가 있으면 Brevo HTTPS API로 발송(포트 차단 걱정 없음, 추천).
# 없고 EMAIL_HOST만 있으면 SMTP로 발송(사내 메일서버 방화벽에 막힐 수 있음).
# 둘 다 없으면 콘솔/로그 출력으로만 남는 모의(mock) 발송으로 자동 대체된다.
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
if EMAIL_HOST:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
    EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
    EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
    EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True') == 'True'
    # 타임아웃을 안 주면 SMTP 서버가 응답 없이 멈출 때 소켓 연결이 무한 대기하다가
    # gunicorn 워커 타임아웃으로 프로세스 전체가 SIGKILL 당한다(요청 실패 정도가
    # 아니라 서버가 죽는 심각한 문제). 짧게 끊어서 notify.py의 예외 처리로 넘긴다.
    EMAIL_TIMEOUT = int(os.environ.get('EMAIL_TIMEOUT', '10'))
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'noreply@nousbo.com')

# 알림 이메일 안의 "요청 상세보기" 버튼 링크에 쓰는 절대주소. SITE_URL을 직접 채우면
# 그 값을 쓰고, 없으면 Render가 자동 주입하는 배포 도메인을 쓰고, 그것도 없으면
# 로컬 개발 서버 주소로 대체한다.
SITE_URL = os.environ.get('SITE_URL', '') or (f'https://{_render_host}' if _render_host else 'http://127.0.0.1:8000')

# 카카오톡/문자 — 뿌리오(비즈뿌리오)에 전용 발신번호를 등록해두면 그쪽을 우선 사용한다.
# 개인 휴대폰 번호를 발신번호로 쓰면 통신사 "번호도용문자차단서비스"에 걸려 API 호출은
# 성공해도 실제 문자가 도착하지 않는 문제가 있어, 전용 회선으로 전환하며 도입했다.
PPURIO_ACCOUNT = os.environ.get('PPURIO_ACCOUNT', '')
PPURIO_API_SECRET = os.environ.get('PPURIO_API_SECRET', '')  # 비즈뿌리오 계정 비밀번호
PPURIO_SENDER_PHONE = os.environ.get('PPURIO_SENDER_PHONE', '')  # 뿌리오에 사전 등록된 발신번호

# 솔라피(Solapi) — 뿌리오 설정이 없을 때의 대체 경로로 유지한다(레거시). 카카오 알림톡은
# 이 시스템이 상황별로 자유 문구를 만들어 보내는 구조라 이벤트마다 별도 템플릿을 등록하는
# 대신, 승인받은 "일반 알림 템플릿" 하나(예: 본문에 #{message} 변수 하나만 있는 형태)에
# KAKAO_PF_ID + KAKAO_TEMPLATE_ID를 채우면 그 템플릿으로 발송한다 — 뿌리오·솔라피·알림톡
# 설정이 모두 없으면 로그만 남는 모의(mock) 발송으로 자동 대체된다.
SOLAPI_API_KEY = os.environ.get('SOLAPI_API_KEY', '')
SOLAPI_API_SECRET = os.environ.get('SOLAPI_API_SECRET', '')
SOLAPI_SENDER_PHONE = os.environ.get('SOLAPI_SENDER_PHONE', '')  # 솔라피에 사전 등록된 발신번호
KAKAO_PF_ID = os.environ.get('KAKAO_PF_ID', '')  # 카카오톡 채널 발신프로필 키
KAKAO_TEMPLATE_ID = os.environ.get('KAKAO_TEMPLATE_ID', '')  # 승인된 알림톡 템플릿 코드(변수 #{message} 하나만 사용)

# 웹 푸시 — 브라우저 자체 기능(FCM/APNs 등은 그 뒤에서 각 브라우저가 알아서 씀,
# 별도 계정 가입 불필요). VAPID_PUBLIC_KEY/PRIVATE_KEY가 있으면 담당자가 "브라우저
# 알림 받기"를 켰을 때 실제 발송된다. 없으면 그 기능 자체가 화면에 나타나지 않는다.
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_CLAIM_EMAIL = os.environ.get('VAPID_CLAIM_EMAIL', 'mailto:noreply@nousbo.com')

# AI 비서(채팅으로 발주 현황·이력·파일을 물어보는 기능) — 구글 Gemini API 무료 티어 사용.
# GEMINI_API_KEY가 없으면 화면(nav)에 링크 자체가 나타나지 않는다(설정 없이도 앱은 정상 동작).
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '').strip()
# render.yaml에 sync:false로 선언된 변수는 대시보드에서 값을 안 채우면 "빈 문자열"로
# 존재할 수 있다 — 이때 get(키, 기본값)은 기본값이 아니라 ''를 돌려주고, 그대로 두면
# 요청 URL이 .../models/:generateContent 가 되어 매번 404가 난다. or로 받아 빈 값도
# 기본 모델로 되돌린다.
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', '').strip() or 'gemini-3.7-flash'

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
        # Django 기본 설정은 django.request의 콘솔 출력을 require_debug_true로 걸러서,
        # 운영(DEBUG=False)에서는 500 오류의 트레이스백이 아무 데도 남지 않는다 —
        # 화면에는 "Internal Server Error"만 뜨고 원인은 사라진다. 명시적으로 콘솔에
        # 남겨서 Render 대시보드 Logs 탭에서 그대로 확인할 수 있게 한다.
        'django.request': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
        # AI 비서가 Gemini에서 받은 실제 오류(사용량 초과/키/모델명)도 같은 곳에 남긴다.
        'workflow.assistant': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}
