from django.db import models
from django.utils import timezone


class TargetDomain(models.Model):
    name = models.CharField(max_length=100, unique=True, verbose_name="Tên Domain (vd: topcv, vnworks)")
    is_active = models.BooleanField(default=True, verbose_name="Trạng thái hoạt động")
    is_harvest_enabled = models.BooleanField(
        default=True,
        verbose_name="Bật Harvester",
        help_text="Cho phép domain này thu thập link mới.",
    )
    is_extract_enabled = models.BooleanField(
        default=True,
        verbose_name="Bật Extractor",
        help_text="Cho phép domain này bóc tách chi tiết job.",
    )
    harvest_runs_per_day = models.PositiveIntegerField(
        default=1,
        verbose_name="Số lần chạy Harvester/ngày",
        help_text="Số lần thu thập link mới trong 1 ngày cho domain này.",
    )
    extract_runs_per_day = models.PositiveIntegerField(
        default=24,
        verbose_name="Số lần chạy Extractor/ngày",
        help_text="Số lần bóc tách chi tiết job trong 1 ngày cho domain này.",
    )
    job_read_time_seconds = models.PositiveIntegerField(
        default=3,
        verbose_name="Thời gian đọc job (giây)",
        help_text="Thời gian chờ/giả lập đọc trang job detail trước khi extract.",
    )
    max_pages_per_keyword = models.PositiveIntegerField(
        default=5,
        verbose_name="Số trang tối đa/keyword",
        help_text="Giới hạn số trang danh sách việc làm được crawl cho mỗi keyword.",
    )
    max_jobs_per_keyword = models.PositiveIntegerField(
        default=100,
        verbose_name="Số job tối đa/keyword",
        help_text="Giới hạn tổng số job URL được lưu cho mỗi keyword trong một vòng harvest.",
    )
    search_locations = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Địa điểm tìm kiếm",
        help_text='Danh sách location cho search, ví dụ: ["Vietnam", "Ho Chi Minh City, Vietnam"].',
    )
    extract_batch_size = models.PositiveIntegerField(
        default=3,
        verbose_name="Batch size Extractor",
        help_text="Số link PENDING tối đa được lock và xử lý trong một lần chạy extractor.",
    )
    request_delay_min_seconds = models.PositiveIntegerField(
        default=1,
        verbose_name="Delay nhỏ nhất/request (giây)",
        help_text="Thời gian chờ ngẫu nhiên tối thiểu sau khi trang load.",
    )
    request_delay_max_seconds = models.PositiveIntegerField(
        default=3,
        verbose_name="Delay lớn nhất/request (giây)",
        help_text="Thời gian chờ ngẫu nhiên tối đa sau khi trang load.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({'Active' if self.is_active else 'Inactive'})"


class Keyword(models.Model):
    name = models.CharField(max_length=255, unique=True, verbose_name="Từ khóa")
    is_active = models.BooleanField(default=True, verbose_name="Trạng thái hoạt động")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class ProxyConfig(models.Model):
    name = models.CharField(max_length=100, verbose_name="Tên proxy")
    proxy_url = models.URLField(
        max_length=1000,
        verbose_name="Proxy URL",
        help_text="Định dạng: http://user:pass@host:port hoặc https://user:pass@host:port.",
    )
    domain = models.ForeignKey(
        TargetDomain,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        db_constraint=False,
        related_name='proxy_configs',
        verbose_name="Domain",
        help_text="Để trống nếu proxy này dùng chung cho mọi domain.",
    )
    is_active = models.BooleanField(default=True, verbose_name="Trạng thái hoạt động")
    priority = models.PositiveIntegerField(
        default=100,
        verbose_name="Độ ưu tiên",
        help_text="Số nhỏ hơn được ưu tiên dùng trước.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        scope = self.domain.name if self.domain_id else 'global'
        return f"{self.name} ({scope})"

    class Meta:
        ordering = ['priority', 'id']
        indexes = [
            models.Index(fields=['is_active', 'domain', 'priority']),
        ]
        verbose_name = "Proxy"
        verbose_name_plural = "Proxies"


class DomainRunLog(models.Model):
    MODE_CHOICES = (
        ('HARVEST', 'Harvest'),
        ('EXTRACT', 'Extract'),
    )
    STATUS_CHOICES = (
        ('STARTED', 'Started'),
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
    )

    domain = models.CharField(max_length=100, verbose_name="Domain")
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, verbose_name="Loại worker")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='STARTED', verbose_name="Trạng thái")
    run_date = models.DateField(db_index=True, verbose_name="Ngày chạy")
    run_number = models.PositiveIntegerField(verbose_name="Lần chạy trong ngày")
    configured_runs = models.PositiveIntegerField(verbose_name="Tổng số lần cấu hình/ngày")
    started_at = models.DateTimeField(default=timezone.now, db_index=True, verbose_name="Bắt đầu")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Kết thúc")
    items_count = models.PositiveIntegerField(default=0, verbose_name="Số item xử lý")
    error_message = models.TextField(blank=True, default='', verbose_name="Lỗi")

    def __str__(self):
        return f"{self.domain} {self.mode} {self.run_date} #{self.run_number}/{self.configured_runs}"

    class Meta:
        ordering = ['-started_at', '-id']
        indexes = [
            models.Index(fields=['domain', 'mode', 'run_date', 'status']),
        ]
        verbose_name = "Lịch sử chạy domain"
        verbose_name_plural = "Lịch sử chạy domain"


class JobLink(models.Model):
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
    )

    url = models.URLField(max_length=1000, unique=True, verbose_name="URL Công việc")
    keyword = models.CharField(max_length=255, verbose_name="Từ khóa")
    domain = models.CharField(max_length=100, verbose_name="Domain")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING', verbose_name="Trạng thái")
    tried_count = models.IntegerField(default=0, verbose_name="Số lần thử")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.domain} - {self.url}"

    class Meta:
        indexes = [
            models.Index(fields=['status', 'domain']),
        ]


class JobDetail(models.Model):
    job_url = models.URLField(max_length=1000, unique=True, verbose_name="URL Công việc")
    title = models.CharField(max_length=500, null=True, blank=True, verbose_name="Tiêu đề công việc")
    company_name = models.CharField(max_length=255, null=True, blank=True, verbose_name="Tên công ty")
    company_url = models.URLField(max_length=1000, null=True, blank=True, verbose_name="Link công ty")
    contract_type = models.CharField(max_length=100, null=True, blank=True, verbose_name="Loại hợp đồng")
    deadline = models.CharField(max_length=100, null=True, blank=True, verbose_name="Hạn nộp hồ sơ")
    description = models.TextField(null=True, blank=True, verbose_name="Mô tả công việc")
    experience_level = models.CharField(max_length=100, null=True, blank=True, verbose_name="Kinh nghiệm")
    location = models.CharField(max_length=255, null=True, blank=True, verbose_name="Địa điểm")
    posted_time = models.CharField(max_length=100, null=True, blank=True, verbose_name="Thời gian đăng")
    salary = models.CharField(max_length=255, null=True, blank=True, verbose_name="Mức lương")
    sector = models.CharField(max_length=255, null=True, blank=True, verbose_name="Ngành nghề")
    scraped_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title if self.title else f"JobDetail #{self.id}"
