# UnderMB

動画を指定 MB 以下に圧縮するツールです。X などへの投稿向け。

## ブラウザ版（一般公開）

静的サイトです。**動画はサーバーに送らず、ブラウザ内（FFmpeg.wasm）で圧縮**します。

- 公開 URL: https://iroiro296.github.io/undermb/
- リポジトリ: https://github.com/iroiro296/undermb
- ローカル確認: ルートで静的サーバを起動（`npx serve .` など）

## CLI 版（ローカル高速）

`cli/` に Python + 本体 FFmpeg 版があります。

```powershell
python cli/compress.py video.mp4 -s 512
cli\gui.bat
```

## デプロイ

GitHub Pages / Cloudflare Pages / Netlify / Vercel いずれもルートを公開すれば動きます。  
`_headers` / `netlify.toml` / `vercel.json` で COOP/COEP を付与します（GitHub Pages は `coi-serviceworker.js` で代替）。
