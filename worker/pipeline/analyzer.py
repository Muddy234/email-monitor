"""Email text utilities."""


def _strip_quoted_content(body):
    """Strip quoted reply chains, keeping only the newest message."""
    for marker in ["From:", "-----Original Message", "________________________________"]:
        idx = body.find(marker)
        if idx > 0:
            return body[:idx].rstrip()
    return body
