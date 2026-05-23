from urllib.parse import urlsplit, urlunsplit

def remove_query_and_fragment(url: str) -> str:
    """
    Xóa query và fragment khỏi URL.
    Ví dụ: https://www.topcv.vn/tim-viec-lam-ky-su-xay-dung?page=5 -> https://www.topcv.vn/tim-viec-lam-ky-su-xay-dung
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))