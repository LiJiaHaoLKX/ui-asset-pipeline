"""Two sequential generation requests differing only in size; retain raw images."""
import hashlib
import argparse
import json
import urllib.request
from datetime import datetime
from pathlib import Path
from PIL import Image
from gpt_image_api import read_image_config, _send, _save_result

PROMPT = '''Generate one image of a finished Chinese psychological counseling discovery homepage. Show the complete application interface directly, with no device frame or presentation board. Use a gentle pale blue and lavender background, white cards, blue primary actions, restrained purple accents, dark navy text, crisp Chinese typography and consistent rounded icons.

At the top, show a status bar, a circular user portrait, the greeting “小雨，你好”, the subtitle “今天，也记得关心自己”, and a mini-program capsule. Below, place a search field labeled “搜索问题 / 心理师” with a blue “搜索” button.

Show ten category entrances arranged in two rows: “心理健康”, “恋爱情感”, “人际关系”, “情绪管理”, “婚姻家庭”, “职场心理”, “个人成长”, “亲子教育”, “学业心理”, “全部分类”. Use matching blue and purple line icons with pale circular backgrounds.

Add a pale blue-lavender assessment card with a decorative lavender orb, the title “AI 测评”, heading “了解此刻的心理状态”, text “情绪状态 · 压力情况 · 人际关系”, a blue “开始测评” button, a “测评记录” link, and the note “测评结果用于自我了解，不作为医学诊断”.

Below “推荐心理师” and “查看全部”, show two complete counselor cards with natural photographic portraits. The first reads “林悦”, “资深”, “资质认证”, “从业8年 · 好评率99% · 上海”, “陪你梳理情绪，找回内心的力量”, “情绪管理”, “恋爱情感”, “¥300起”, “50分钟/次”, and “预约”. The second reads “陈安”, “资深”, “资质认证”, “从业6年 · 暂无评价 · 北京”, “理解关系中的困扰，探索新的可能”, “人际关系”, “职场心理”, “¥260起”, “50分钟/次”, and “预约”. Use a friendly woman and a composed man for the respective portraits.

Finish with a bottom navigation labeled “首页”, “咨询”, “消息”, “我的”, with “首页” selected in blue. Balance the spacing and fit all visible components naturally into the image. Keep all text, buttons and cards fully visible, with no clipping or overlap.'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--compare-prompt-dimensions', action='store_true')
    parser.add_argument('--conflicting-dimensions', action='store_true')
    parser.add_argument('--custom-size-test', action='store_true')
    parser.add_argument('--white-image-test', action='store_true')
    parser.add_argument('--white-image-explicit-size-test', action='store_true')
    parser.add_argument('--white-square-test', action='store_true')
    parser.add_argument('--white-size', help='Generate one white image with this size in both API and prompt')
    parser.add_argument('--model', help='Override model for this experiment only')
    parser.add_argument('--page-size', help='Generate one counseling homepage at this API size')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = read_image_config()
    endpoint = config['GPT_IMAGE_BASE_URL'].rstrip('/') + '/images/generations'
    if endpoint != 'https://wawazz.xyz/v1/images/generations':
        raise RuntimeError('Endpoint changed; stop comparison')
    template = json.loads((root / 'pages/page-003/runs/gpt-image/reference-web-20260917-104821/request.json').read_text(encoding='utf-8'))
    template['prompt'] = PROMPT
    if args.model:
        template['model'] = args.model
    prefix = 'prompt-dimensions-' if args.compare_prompt_dimensions else 'size-comparison-'
    if args.conflicting_dimensions:
        prefix = 'conflicting-dimensions-'
    if args.custom_size_test:
        prefix = 'custom-size-'
    if args.white_image_test:
        prefix = 'white-image-'
    if args.white_image_explicit_size_test:
        prefix = 'white-image-explicit-size-'
    if args.white_square_test:
        prefix = 'white-square-'
    if args.white_size:
        prefix = 'white-custom-'
    if args.page_size:
        prefix = 'counseling-page-'
    run = root / 'runs' / (prefix + datetime.now().strftime('%Y%m%d-%H%M%S'))
    run.mkdir(parents=True)
    (run / 'prompt.txt').write_text(PROMPT, encoding='utf-8')
    print('Experiment:', run, flush=True)
    results = []
    variants = [('1024x1024', '1024x1024', PROMPT), ('1024x1536', '1024x1536', PROMPT)]
    if args.compare_prompt_dimensions:
        variants = [('A-no-dimensions', '1024x1536', PROMPT),
                    ('B-explicit-dimensions', '1024x1536', PROMPT + '\n\nThe image dimensions must be exactly 1024 by 1536 pixels.')]
    if args.conflicting_dimensions:
        variants = [('api-portrait-prompt-landscape', '1024x1536',
                     PROMPT + '\n\nThe image dimensions must be exactly 1536 by 1024 pixels.')]
    if args.custom_size_test:
        variants = [('750x1334-no-dimensions', '750x1334', PROMPT)]
    if args.white_image_test:
        variants = [('750x1334-white', '750x1334', '生成一张纯白色图片')]
    if args.white_image_explicit_size_test:
        variants = [('750x1334-white-explicit-size', '750x1334', '生成一张750x1334像素的纯白色图片')]
    if args.white_square_test:
        variants = [('1024x1024-white', '1024x1024', '生成一张1024x1024像素的纯白色图片')]
    if args.white_size:
        variants = [(args.white_size + '-white', args.white_size, '生成一张' + args.white_size + '像素的纯白色图片')]
    if args.page_size:
        variants = [(args.page_size + '-page', args.page_size, PROMPT)]
    for label, size, prompt in variants:
        folder = run / label
        folder.mkdir()
        payload = {**template, 'size': size, 'prompt': prompt}
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        (folder / 'request.json').write_bytes(raw)
        (folder / 'prompt.txt').write_text(prompt, encoding='utf-8')
        print('Generating:', label, size, payload['model'], payload['quality'], flush=True)
        result = {'variant': label, 'requestedSize': size, 'promptSHA256': hashlib.sha256(prompt.encode()).hexdigest()}
        try:
            request = urllib.request.Request(endpoint, data=raw, method='POST', headers={
                'Authorization': 'Bearer ' + config['GPT_IMAGE_API_KEY'], 'Content-Type': 'application/json'})
            body, content_type = _send(request, folder, 300)
            output = folder / 'original.png'
            _save_result(body, content_type, output, folder)
            with Image.open(output) as image:
                result['actualSize'] = list(image.size)
                image.verify()
            result['imageFile'] = str(output)
            result['postProcessing'] = 'none'
        except Exception as error:
            result['error'] = str(error)
        results.append(result)
        (run / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
