"""Safe, evidence-based request failure details for the browser."""
import json
import re
import urllib.error
from urllib.parse import urlsplit


def diagnose(error, stage, url='', elapsed=None, headers=None, body=b''):
    parts = urlsplit(url)
    endpoint = f'{parts.scheme}://{parts.hostname or ""}' + (f':{parts.port}' if parts.port else '') + parts.path if url else ''
    status = error.code if isinstance(error, urllib.error.HTTPError) else None
    if status:
        origin = '远端 HTTP 错误响应'
        conclusion = '已收到远端错误状态码，不是本地等待超时。仅凭响应无法确定中转站、边缘网关或上游模型中哪一层负责。'
    elif isinstance(error, ConnectionResetError) or getattr(error, 'winerror', None) == 10054 or (isinstance(error, urllib.error.URLError) and (isinstance(error.reason, ConnectionResetError) or getattr(error.reason, 'winerror', None) == 10054)):
        origin = '远端连接被关闭'
        conclusion = '连接在收到有效 HTTP 响应前被对端关闭，因此没有 HTTP 状态码；不能证明请求已被模型处理。'
    elif isinstance(error, (TimeoutError, urllib.error.URLError)):
        origin = '本地等待或网络连接失败'
        conclusion = '未收到有效 HTTP 响应；不能据此认定中转站或模型生成失败。'
    elif stage == '任务执行（未记录更细阶段）':
        origin = '来源待确认'
        conclusion = '该任务未记录足够的请求阶段信息，不能仅凭错误文字判定是本地还是远端。'
    else:
        origin = '本地处理异常'
        conclusion = '错误发生在本地处理阶段；可能由响应格式或图片内容不符合预期引起，请结合阶段排查。'
    excerpt = body.decode('utf-8', errors='replace')[:3000]
    excerpt = re.sub(r'(?i)Bearer\s+\S+|sk-[\w-]+', '[已隐藏密钥]', excerpt)
    excerpt = re.sub(r'(?i)([?&](?:key|token|signature|api_key)=)[^\s"&<>]+', r'\1[已隐藏]', excerpt)
    selected = {key: str(headers.get(key)) for key in ('content-type', 'server', 'cf-ray', 'x-request-id', 'x-oneapi-request-id', 'date') if headers and headers.get(key)}
    return {'origin': origin, 'stage': stage, 'endpoint': endpoint, 'httpStatus': status,
            'elapsedSeconds': round(elapsed, 2) if elapsed is not None else None,
            'responseHeaders': selected, 'responseExcerpt': excerpt, 'conclusion': conclusion}


def record(error, folder, **kwargs):
    detail = diagnose(error, **kwargs)
    detail['recordPath'] = str(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'diagnostic.json').write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding='utf-8')
    error.diagnostic = detail
    return detail
