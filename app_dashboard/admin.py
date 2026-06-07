from datetime import date
from urllib.parse import urlsplit, urlunsplit

from django.contrib import admin
from django.contrib.admin.sites import NotRegistered
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils.html import format_html
from unfold.admin import ModelAdmin

from .models import TargetDomain, Keyword, ProxyConfig, DomainRunLog, JobLink, JobDetail

for model in (get_user_model(), Group):
    try:
        admin.site.unregister(model)
    except NotRegistered:
        pass


class DashboardModelAdmin(ModelAdmin):
    change_list_template = 'admin/app_dashboard/change_list_scroll.html'
    actions_on_top = False
    actions_on_bottom = True
    actions_selection_counter = False
    list_horizontal_scrollbar_top = True
    list_fullwidth = True

    class Media:
        css = {
            'all': ('app_dashboard/css/admin_submit_loading.css',),
        }
        js = ('app_dashboard/js/admin_submit_loading.js',)


class DateRangeFilterAdmin(DashboardModelAdmin):
    date_field = None
    date_from_param = 'date_from'
    date_to_param = 'date_to'
    search_placeholder = 'Tìm kiếm...'
    date_filter_label = 'Ngày'

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
                'search_placeholder': self.search_placeholder,
                'date_filter_label': self.date_filter_label,
            }
        )
        return super().changelist_view(request, extra_context=extra_context)

@admin.register(TargetDomain)
class TargetDomainAdmin(DashboardModelAdmin):
    list_display = (
        'name',
        'is_active',
        'is_harvest_enabled',
        'is_extract_enabled',
        'is_proxy_enabled',
        'created_at',
    )
    list_editable = (
        'is_active',
        'is_harvest_enabled',
        'is_extract_enabled',
        'is_proxy_enabled',
    )
    search_fields = ('name',)

    @admin.display(description='Địa điểm tìm kiếm')
    def display_search_locations(self, obj):
        locations = obj.search_locations or []
        if isinstance(locations, list):
            return ', '.join(str(location) for location in locations)
        return str(locations)

@admin.register(Keyword)
class KeywordAdmin(DashboardModelAdmin):
    list_display = ('name', 'is_active', 'created_at')
    list_editable = ('is_active',)
    search_fields = ('name',)

@admin.register(ProxyConfig)
class ProxyConfigAdmin(DashboardModelAdmin):
    list_display = ('name', 'domain', 'display_proxy_url', 'is_active', 'priority', 'updated_at')
    list_editable = ('is_active', 'priority')
    list_filter = ('is_active', 'domain')
    search_fields = ('name', 'proxy_url', 'domain__name')
    autocomplete_fields = ('domain',)

    @admin.display(description='Proxy URL')
    def display_proxy_url(self, obj):
        parts = urlsplit(obj.proxy_url)
        if not parts.username and not parts.password:
            return obj.proxy_url

        host = parts.hostname or ''
        if ':' in host and not host.startswith('['):
            host = f'[{host}]'
        port = f':{parts.port}' if parts.port else ''
        netloc = f'***:***@{host}{port}'
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@admin.register(DomainRunLog)
class DomainRunLogAdmin(DateRangeFilterAdmin):
    list_display = (
        'domain',
        'mode',
        'status',
        'run_date',
        'display_daily_runs',
        'started_at',
        'finished_at',
    )
    list_filter = ('mode', 'status', 'domain', 'run_date')
    search_fields = ('domain', 'error_message')
    date_field = 'started_at'
    search_placeholder = 'Tìm domain hoặc lỗi...'
    date_filter_label = 'Ngày bắt đầu'
    readonly_fields = (
        'domain',
        'mode',
        'status',
        'run_date',
        'run_number',
        'configured_runs',
        'started_at',
        'finished_at',
        'error_message',
    )

    @admin.display(description='Lần chạy/ngày')
    def display_daily_runs(self, obj):
        return f'{obj.run_number}/{obj.configured_runs}'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(JobLink)
class JobLinkAdmin(DateRangeFilterAdmin):
    list_display = ('display_url', 'domain', 'keyword', 'status', 'tried_count', 'created_at', 'updated_at')
    list_filter = ('status', 'domain', 'keyword')
    search_fields = ('url', 'keyword')
    date_field = 'created_at'
    search_placeholder = 'Tìm URL hoặc keyword...'
    date_filter_label = 'Ngày tạo'
    change_list_template = 'admin/app_dashboard/joblink/change_list.html'

    @admin.display(description='URL Công việc', ordering='url')
    def display_url(self, obj):
        return format_html(
            '<span title="{}" style="display:block; max-width:200px; overflow:hidden; '
            'text-overflow:ellipsis; white-space:nowrap;">{}</span>',
            obj.url,
            obj.url,
        )

@admin.register(JobDetail)
class JobDetailAdmin(DateRangeFilterAdmin):
    list_display = ('title', 'company_name', 'location', 'salary', 'deadline', 'scraped_at')
    search_fields = ('title', 'company_name', 'location', 'job_url')
    date_field = 'scraped_at'
    search_placeholder = 'Tìm tiêu đề, công ty, địa điểm hoặc URL...'
    date_filter_label = 'Ngày scrape'
    change_list_template = 'admin/app_dashboard/jobdetail/change_list.html'
