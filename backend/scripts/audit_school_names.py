"""Read-only H9 evidence. Never prints credentials; JSON report only."""
import json
import re
from pathlib import Path
import psycopg2


def main():
    values = {}
    for line in Path.home().joinpath('.hermes/secrets/softeedashboard.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            values[k.strip()] = v.strip().strip('\"\'')
    try:
        conn = psycopg2.connect(values['DB_URL'], sslmode='require',
            options='-c default_transaction_read_only=on -c statement_timeout=30000', connect_timeout=20)
        with conn, conn.cursor() as cur:
            cur.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public' AND table_name IN ('route_timed_stops','stop_clusters') ORDER BY table_name, ordinal_position")
            columns = cur.fetchall()
            cur.execute("SELECT r.route_cluster_id, r.dow, r.stop_order, r.stop_cluster_id, r.address, s.address, r.arrive, r.generated_at FROM public.route_timed_stops r LEFT JOIN public.stop_clusters s ON s.stop_cluster_id=r.stop_cluster_id WHERE concat_ws(' ',r.address,s.address) ~* '(school|elementary|academy|prep|montessori|\\mElem\\M)' ORDER BY r.route_cluster_id,r.dow,r.stop_order")
            rows = cur.fetchall()
            cur.execute("SHOW transaction_read_only")
            readonly = cur.fetchone()[0]
        conn.close()
    except Exception as exc:
        print(json.dumps({'error_type': type(exc).__name__}))
        raise SystemExit(1)
    old = lambda x: 'school' in str(x or '').lower() or 'elementary' in str(x or '').lower()
    print(json.dumps({'read_only': readonly, 'columns': columns, 'school_like_rows': rows,
        'unrecognized_rows': [r for r in rows if not old(r[4])]}, default=str, indent=2))

if __name__ == '__main__':
    main()
