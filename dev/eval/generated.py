"""Sample files too large to paste, made the same way every time (a case's `files: [{generate: {kind, ...}}]`)."""
import random
from datetime import datetime, timedelta, timezone

USERS = ['alice', 'bob', 'carol', 'dave', 'erin', 'frank', 'grace', 'heidi', 'ivan', 'judy']
SITES = [('https://intranet.example.org/', 'business'), ('https://news.example.com/world', 'news'),
         ('https://mail.example.org/inbox', 'webmail'), ('https://files.example.net/share/q3.xlsx', 'file-sharing'),
         ('https://cdn.example.com/lib.js', 'technology'), ('http://198.51.100.40/update.bin', 'uncategorised'),
         ('https://social.example.com/feed', 'social-media'), ('https://search.example.com/?q=stroom', 'search')]
METHODS = ['GET'] * 8 + ['POST', 'CONNECT']
STATUSES = [200] * 12 + [204, 301, 302, 304, 403, 404, 407, 502]


def proxy_access(day: str, records: int, seed: int) -> str:
    """A web proxy's access log for one day, CSV with a header: one record per request, about 120 bytes each."""
    rng = random.Random(seed)
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    lines = ['time,client_ip,user,method,url,status,bytes,category,action']
    for n in range(records):
        at = start + timedelta(seconds=n * 86400 / records)
        url, category = rng.choice(SITES)
        status = rng.choice(STATUSES)
        action = 'blocked' if status in (403, 407) else 'allowed'
        lines.append(f"{at.strftime('%Y-%m-%dT%H:%M:%S')}Z,10.{rng.randint(0, 3)}.{rng.randint(0, 254)}.{rng.randint(1, 254)},"
                     f"{rng.choice(USERS)},{rng.choice(METHODS)},{url},{status},{rng.randint(200, 900000)},{category},{action}")
    return '\n'.join(lines) + '\n'
