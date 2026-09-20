"""Replay a saved image request once, without resizing or modifying the prompt."""
import argparse
import hashlib
import json
import urllib.request
from datetime import datetime
from pathlib import Path
from PIL import Image
from gpt_image_api import read_image_config, _send, _save_result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('request', type=Path)
    parser.add_argument('--prompt-file', type=Path)
    args = parser.parse_args()
    raw = args.request.read_bytes()
    payload = json.loads(raw)
    if args.prompt_file:
        payload['prompt'] = args.prompt_file.read_text(encoding='utf-8').strip()
        raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    config = read_image_config()
    endpoint = config['GPT_IMAGE_BASE_URL'].rstrip('/') + '/images/generations'
    if endpoint != 'https://wawazz.xyz/v1/images/generations':
        raise RuntimeError('Current endpoint differs from the original comparison endpoint')
    root = Path(__file__).resolve().parents[1]
    run = root / 'runs' / ('cli-replay-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    run.mkdir(parents=True, exist_ok=False)
    (run / 'request.json').write_bytes(raw)
    print('Record directory:', run, flush=True)
    print('Endpoint:', endpoint, flush=True)
    print(json.dumps({k: payload.get(k) for k in ('model', 'size', 'quality', 'n', 'output_format')}), flush=True)
    req = urllib.request.Request(endpoint, data=raw, method='POST', headers={
        'Authorization': 'Bearer ' + config['GPT_IMAGE_API_KEY'], 'Content-Type': 'application/json'})
    body, content_type = _send(req, run, 300)
    output = run / 'original-image.png'
    _save_result(body, content_type, output, run)
    with Image.open(output) as image:
        dimensions = image.size
        image.verify()
    report = {'endpoint': endpoint, 'requestFile': str(args.request.resolve()),
              'requestSHA256': hashlib.sha256(raw).hexdigest(),
              'actualWidth': dimensions[0], 'actualHeight': dimensions[1],
              'imageSHA256': hashlib.sha256(output.read_bytes()).hexdigest(),
              'postProcessing': 'none', 'imageFile': str(output)}
    (run / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
