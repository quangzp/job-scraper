job_platform/                # Thư mục gốc của toàn bộ dự án
│
├── .env                     # File chứa biến môi trường (DB_URL, Proxy, Secret Key)
├── requirements.txt         # Danh sách thư viện (Django, crawlee, psycopg2...)
├── Dockerfile               # File build image duy nhất (dựa trên mcr.microsoft.com/playwright/python)
├── docker-compose.yml       # Khởi chạy 4 container (Postgres, Web, Harvester, Extractor)
├── manage.py                # File thực thi lệnh mặc định của Django
│
├── core/                    # 1. DJANGO PROJECT CORE (Cấu hình lõi)
│   ├── __init__.py
│   ├── settings.py          # Cấu hình Database kết nối tới Postgres, timezone, installed apps
│   ├── urls.py              # Điều hướng URL (Map vào trang Admin)
│   ├── wsgi.py              # File chạy server web (đồng bộ)
│   └── asgi.py              # File chạy server web (bất đồng bộ - tùy chọn)
│
├── app_dashboard/           # 2. DJANGO APP (Quản trị Web & Database Schema)
│   ├── __init__.py
│   ├── admin.py             # Cấu hình giao diện Control Panel cực xịn cho khách
│   ├── apps.py
│   ├── models.py            # ĐỊNH NGHĨA BẢNG DB: JobLink (State), JobDetail, Keyword
│   └── views.py             # (Chỉ dùng nếu bạn muốn code thêm API/Dashboard ngoài Admin)
│
└── scrapers/                # 3. SCRAPING ENGINE (Lõi Crawlee Python)
    ├── __init__.py
    ├── run_worker.py        # [QUAN TRỌNG] File cầu nối có chứa `django.setup()` để gọi ORM
    │
    ├── config/              
    │   └── selectors.json   # Nơi chứa MỌI XPath/CSS của 5 domain (Không hardcode vào script)
    │
    ├── utils/               
    │   ├── text_cleaner.py  # Chứa hàm Regex: Xóa HTML tag, bóc tách số tiền lương, format ngày
    │   └── proxy.py         # Hàm random proxy nếu có dùng
    │
    ├── harvesters/          # PHASE 1: Thu thập URL (Lưu vào DB với status PENDING)
    │   ├── base.py          # Class BaseHarvester chứa logic insert DB
    │   ├── topcv.py         # Quét danh sách phân trang TopCV
    │   └── vnworks.py       # Quét danh sách phân trang VNWorks
    │
    └── extractors/          # PHASE 2: Bóc data (Lấy link PENDING từ DB -> Cào -> Lưu JobDetail)
        ├── base.py          # Class BaseExtractor chứa logic khóa dòng DB, update status
        ├── topcv.py         # PlaywrightCrawler kết hợp selectors.json bóc JD TopCV
        └── vnworks.py       # PlaywrightCrawler kết hợp selectors.json bóc JD VNWorks