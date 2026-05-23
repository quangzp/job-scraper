from datetime import date

from django.contrib import admin
from .models import TargetDomain, Keyword, JobLink, JobDetail


class DateRangeFilterAdmin(admin.ModelAdmin):
    date_field = None
    date_from_param = 'date_from'
    date_to_param = 'date_to'

    def _parse_date(self, value: str):
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if not self.date_field:
            return queryset

        raw_date_from = getattr(request, '_date_range_from', request.GET.get(self.date_from_param, ''))
        raw_date_to = getattr(request, '_date_range_to', request.GET.get(self.date_to_param, ''))
        date_from = self._parse_date(raw_date_from)
        date_to = self._parse_date(raw_date_to)

        if date_from:
            queryset = queryset.filter(**{f'{self.date_field}__date__gte': date_from})
        if date_to:
            queryset = queryset.filter(**{f'{self.date_field}__date__lte': date_to})

        return queryset

    def changelist_view(self, request, extra_context=None):
        raw_date_from = request.GET.get(self.date_from_param, '')
        raw_date_to = request.GET.get(self.date_to_param, '')
        request._date_range_from = raw_date_from
        request._date_range_to = raw_date_to

        # Remove custom params before Django ChangeList parses lookup params,
        # otherwise admin treats them as invalid DB lookups and shows Database error.
        cleaned_get = request.GET.copy()
        cleaned_get.pop(self.date_from_param, None)
        cleaned_get.pop(self.date_to_param, None)
        request.GET = cleaned_get

        extra_context = extra_context or {}
        extra_context.update(
            {
                'date_from': raw_date_from,
                'date_to': raw_date_to,
                'date_from_param': self.date_from_param,
                'date_to_param': self.date_to_param,
            }
        )
        return super().changelist_view(request, extra_context=extra_context)

@admin.register(TargetDomain)
class TargetDomainAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'is_active',
        'is_harvest_enabled',
        'is_extract_enabled',
        'harvest_runs_per_day',
        'extract_runs_per_day',
        'max_pages_per_keyword',
        'max_jobs_per_keyword',
        'display_search_locations',
        'extract_batch_size',
        'request_delay_min_seconds',
        'request_delay_max_seconds',
        'job_read_time_seconds',
        'created_at',
    )
    list_editable = (
        'is_active',
        'is_harvest_enabled',
        'is_extract_enabled',
        'harvest_runs_per_day',
        'extract_runs_per_day',
        'max_pages_per_keyword',
        'max_jobs_per_keyword',
        'extract_batch_size',
        'request_delay_min_seconds',
        'request_delay_max_seconds',
        'job_read_time_seconds',
    )
    search_fields = ('name',)

    @admin.display(description='Dia diem tim kiem')
    def display_search_locations(self, obj):
        locations = obj.search_locations or []
        if isinstance(locations, list):
            return ', '.join(str(location) for location in locations)
        return str(locations)

@admin.register(Keyword)
class KeywordAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'created_at')
    list_editable = ('is_active',)
    search_fields = ('name',)

@admin.register(JobLink)
class JobLinkAdmin(DateRangeFilterAdmin):
    list_display = ('url', 'domain', 'keyword', 'status', 'tried_count', 'created_at', 'updated_at')
    list_filter = ('status', 'domain', 'keyword')
    search_fields = ('url',)
    date_field = 'created_at'
    change_list_template = 'admin/app_dashboard/joblink/change_list.html'

@admin.register(JobDetail)
class JobDetailAdmin(DateRangeFilterAdmin):
    list_display = ('title', 'company_name', 'location', 'salary', 'deadline', 'scraped_at')
    search_fields = ('title', 'company_name', 'location', 'job_url')
    date_field = 'scraped_at'
    change_list_template = 'admin/app_dashboard/jobdetail/change_list.html'
