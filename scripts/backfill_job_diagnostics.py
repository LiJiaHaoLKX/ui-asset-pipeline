"""Preserve existing failed reference jobs before a service restart. No model calls."""
import json
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from request_diagnostics import diagnose


def main():
    root = Path(__file__).resolve().parents[1]
    with urllib.request.urlopen('http://127.0.0.1:8767/api/state') as response:
        state = json.load(response)
    if any(j['status'] in ('queued', 'running') for j in state['jobs']):
        raise RuntimeError('仍有任务运行，请暂缓重启')
    catalog = json.loads((root / 'workspace/project.json').read_text(encoding='utf-8'))
    page = next(p for p in catalog['pages'] if p['id'] == catalog['activePageId'])
    page_root = root if page.get('storage') == 'legacy' else root / 'pages' / page['id']
    for job in state['jobs']:
        if job['status'] != 'failed' or job['kind'] != 'generate-reference':
            continue
        stamp = datetime.fromisoformat(job['startedAt']).strftime('%Y%m%d-%H%M%S')
        run = page_root / 'runs/gpt-image' / ('reference-web-' + stamp)
        meta_path = run / 'response-meta.json'
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        if meta.get('status', 0) < 400:
            continue
        body_path = run / 'error-response.txt'
        detail = diagnose(urllib.error.HTTPError('', meta['status'], '', {}, None),
                          stage='图片生成 API 请求（根据历史响应记录）',
                          headers={'content-type': meta.get('contentType', '')},
                          body=body_path.read_bytes() if body_path.exists() else b'')
        detail['recordPath'] = str(run.relative_to(root))
        job['diagnostic'] = detail
        destination = page_root / 'runs/job-failures' / (job['id'] + '.json')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')
        print(job['id'], meta['status'], 'diagnostic preserved')


if __name__ == '__main__':
    main()
