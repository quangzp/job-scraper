from django.db import models
from django.utils import timezone


class TargetDomain(models.Model):
    name = models.CharField(max_length=100, unique=True, verbose_name="Tên Domain (vd: topcv, vnworks)")
    is_active = models.BooleanField(default=True, verbose_name="Trạng thái hoạt động")
    is_harvest_enabled = models.BooleanField(
        default=True,
        verbose_name="Bat Harvester",
        help_text="Cho phep domain nay thu thap link moi.",
    )
    is_extract_enabled = models.BooleanField(
        default=True,
        verbose_name="Bat Extractor",
        help_text="Cho phep domain nay boc tach chi tiet job.",
    )
    harvest_runs_per_day = models.PositiveIntegerField(
        default=1,
        verbose_name="So lan chay Harvester/ngay",
        help_text="So lan thu thap link moi trong 1 ngay cho domain nay.",
    )
    extract_runs_per_day = models.PositiveIntegerField(
        default=24,
        verbose_name="So lan chay Extractor/ngay",
        help_text="So lan boc tach chi tiet job trong 1 ngay cho domain nay.",
    )
    job_read_time_seconds = models.PositiveIntegerField(
        default=3,
        verbose_name="Thoi gian doc job (giay)",
        help_text="Thoi gian cho/gia lap doc trang job detail truoc khi extract.",
    )
    max_pages_per_keyword = models.PositiveIntegerField(
        default=5,
        verbose_name="So trang toi da/keyword",
        help_text="Gioi han so trang danh sach viec lam duoc crawl cho moi keyword.",
    )
    max_jobs_per_keyword = models.PositiveIntegerField(
        default=100,
        verbose_name="So job toi da/keyword",
        help_text="Gioi han tong so job URL duoc luu cho moi keyword trong mot vong harvest.",
    )
    search_locations = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Dia diem tim kiem",
        help_text='Danh sach location cho search, vi du: ["Vietnam", "Ho Chi Minh City, Vietnam"].',
    )
    extract_batch_size = models.PositiveIntegerField(
        default=3,
        verbose_name="Batch size Extractor",
        help_text="So link PENDING toi da duoc lock va xu ly trong mot lan chay extractor.",
    )
    request_delay_min_seconds = models.PositiveIntegerField(
        default=1,
        verbose_name="Delay nho nhat/request (giay)",
        help_text="Thoi gian cho ngau nhien toi thieu sau khi trang load.",
    )
    request_delay_max_seconds = models.PositiveIntegerField(
        default=3,
        verbose_name="Delay lon nhat/request (giay)",
        help_text="Thoi gian cho ngau nhien toi da sau khi trang load.",
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


class JobLink(models.Model):
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
    )

    url = models.URLField(max_length=1000, unique=True, verbose_name="URL Công việc")
    keyword = models.ForeignKey(
        Keyword,
        on_delete=models.CASCADE,
        db_constraint=False,
        related_name='job_links',
        verbose_name="Từ khóa",
    )
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
