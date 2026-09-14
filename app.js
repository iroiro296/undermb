import { FFmpeg } from "https://cdn.jsdelivr.net/npm/@ffmpeg/ffmpeg@0.12.15/dist/esm/index.js";
import {
  fetchFile,
  toBlobURL,
} from "https://cdn.jsdelivr.net/npm/@ffmpeg/util@0.12.2/dist/esm/index.js";

const SIZE_MARGIN = 0.95;
const MIN_VIDEO_BPS = 100_000;
const AUDIO_KBPS = 128;
const WARN_FILE_MB = 800;

const els = {
  dropzone: document.getElementById("dropzone"),
  fileInput: document.getElementById("fileInput"),
  dropTitle: document.getElementById("dropTitle"),
  dropHint: document.getElementById("dropHint"),
  sizeInput: document.getElementById("sizeInput"),
  codecSelect: document.getElementById("codecSelect"),
  compressBtn: document.getElementById("compressBtn"),
  progressBlock: document.getElementById("progressBlock"),
  progressLabel: document.getElementById("progressLabel"),
  progressPct: document.getElementById("progressPct"),
  progressFill: document.getElementById("progressFill"),
  progressBar: document.getElementById("progressBar"),
  status: document.getElementById("status"),
  result: document.getElementById("result"),
  preview: document.getElementById("preview"),
  downloadLink: document.getElementById("downloadLink"),
};

/** @type {File | null} */
let selectedFile = null;
/** @type {FFmpeg | null} */
let ffmpeg = null;
let loadingCore = false;
let objectUrl = null;

function formatMB(bytes) {
  return (bytes / 1024 / 1024).toFixed(2);
}

function setStatus(text) {
  els.status.textContent = text;
}

function setProgress(percent, label) {
  const pct = Math.max(0, Math.min(100, percent));
  els.progressBlock.hidden = false;
  els.progressFill.style.width = `${pct}%`;
  els.progressPct.textContent = `${pct.toFixed(1)}%`;
  els.progressLabel.textContent = label;
  els.progressBar.setAttribute("aria-valuenow", String(Math.round(pct)));
}

function clearResult() {
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }
  els.result.hidden = true;
  els.preview.removeAttribute("src");
  els.downloadLink.removeAttribute("href");
}

function selectFile(file) {
  if (!file) return;
  if (!file.type.startsWith("video/") && !/\.(mp4|mov|mkv|webm|avi|m4v)$/i.test(file.name)) {
    setStatus("動画ファイルを選んでください。");
    return;
  }
  selectedFile = file;
  els.dropzone.classList.add("has-file");
  els.dropTitle.textContent = file.name;
  els.dropHint.textContent = `${formatMB(file.size)} MB · 上限を指定して圧縮`;
  els.compressBtn.disabled = false;
  clearResult();
  setStatus("");
  if (file.size > WARN_FILE_MB * 1024 * 1024) {
    setStatus(
      `大きなファイルです（${formatMB(file.size)} MB）。ブラウザのメモリ不足で失敗することがあります。`,
    );
  }
}

async function ensureFFmpeg(onLoadProgress) {
  if (ffmpeg?.loaded) return ffmpeg;
  if (loadingCore) {
    while (loadingCore) await new Promise((r) => setTimeout(r, 100));
    return ffmpeg;
  }
  loadingCore = true;
  try {
    onLoadProgress?.(5, "エンコーダを読み込み中…");
    const instance = new FFmpeg();
    instance.on("log", ({ message }) => {
      if (message && /error|failed/i.test(message)) {
        console.warn(message);
      }
    });
    bindProgress(instance);
    const baseURL = "https://cdn.jsdelivr.net/npm/@ffmpeg/core@0.12.10/dist/esm";
    onLoadProgress?.(20, "FFmpeg コアを取得中（初回のみ）…");
    await instance.load({
      coreURL: await toBlobURL(`${baseURL}/ffmpeg-core.js`, "text/javascript"),
      wasmURL: await toBlobURL(`${baseURL}/ffmpeg-core.wasm`, "application/wasm"),
    });
    ffmpeg = instance;
    onLoadProgress?.(35, "準備完了");
    return ffmpeg;
  } finally {
    loadingCore = false;
  }
}

function probeDuration(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const video = document.createElement("video");
    video.preload = "metadata";
    video.onloadedmetadata = () => {
      const duration = video.duration;
      URL.revokeObjectURL(url);
      if (!Number.isFinite(duration) || duration <= 0) {
        reject(new Error("動画の長さを取得できませんでした。"));
        return;
      }
      resolve(duration);
    };
    video.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("動画メタデータの読み込みに失敗しました。"));
    };
    video.src = url;
  });
}

function calcVideoBitrate(targetBytes, durationSec) {
  const usableBits = targetBytes * 8 * SIZE_MARGIN;
  const totalBps = usableBits / durationSec;
  const audioBps = AUDIO_KBPS * 1000;
  const videoBps = Math.floor(totalBps - audioBps);
  if (videoBps < MIN_VIDEO_BPS) {
    const minMb = Math.ceil(
      ((MIN_VIDEO_BPS + audioBps) * durationSec) / 8 / 1024 / 1024 / SIZE_MARGIN,
    );
    throw new Error(`この長さの動画には約 ${minMb} MB 以上が必要です。`);
  }
  return videoBps;
}

function inputName(file) {
  const ext = (file.name.split(".").pop() || "mp4").toLowerCase();
  return `input.${ext}`;
}

/** @type {{ start: number, end: number, label: string }} */
const progressRange = { start: 0, end: 1, label: "" };

function bindProgress(ff) {
  ff.on("progress", ({ progress }) => {
    const local = Math.max(0, Math.min(1, Number(progress) || 0));
    const overall = progressRange.start + (progressRange.end - progressRange.start) * local;
    setProgress(overall * 100, progressRange.label);
  });
}

async function encodeOnce(ff, file, outName, videoBps, codec, rangeStart, rangeEnd, label) {
  const inName = inputName(file);
  await ff.writeFile(inName, await fetchFile(file));

  const vcodec = codec === "h265" ? "libx265" : "libx264";
  const args = [
    "-i",
    inName,
    "-c:v",
    vcodec,
    "-b:v",
    String(videoBps),
    "-preset",
    "veryfast",
    "-pix_fmt",
    "yuv420p",
    "-c:a",
    "aac",
    "-b:a",
    `${AUDIO_KBPS}k`,
    "-movflags",
    "+faststart",
  ];
  if (codec === "h264") {
    args.push("-profile:v", "high");
  } else {
    args.push("-tag:v", "hvc1");
  }
  args.push(outName);

  progressRange.start = rangeStart;
  progressRange.end = rangeEnd;
  progressRange.label = label;
  setProgress(rangeStart * 100, label);

  try {
    await ff.exec(args);
  } finally {
    try {
      await ff.deleteFile(inName);
    } catch {
      /* ignore */
    }
  }
}

async function compress() {
  if (!selectedFile) return;

  const targetMb = Number(els.sizeInput.value);
  if (!Number.isFinite(targetMb) || targetMb <= 0) {
    setStatus("上限サイズは正の数（MB）で指定してください。");
    return;
  }

  const targetBytes = Math.floor(targetMb * 1024 * 1024);
  const codec = els.codecSelect.value;
  els.compressBtn.disabled = true;
  clearResult();
  setProgress(0, "開始…");

  try {
    if (selectedFile.size <= targetBytes) {
      setProgress(100, "すでに上限以下");
      setStatus(`すでに ${formatMB(selectedFile.size)} MB で上限以下です。そのまま使えます。`);
      objectUrl = URL.createObjectURL(selectedFile);
      els.preview.src = objectUrl;
      els.downloadLink.href = objectUrl;
      els.downloadLink.download = selectedFile.name;
      els.result.hidden = false;
      return;
    }

    const duration = await probeDuration(selectedFile);
    let videoBps = calcVideoBitrate(targetBytes, duration);

    const ff = await ensureFFmpeg((p, label) => setProgress(p, label));
    const outName = "output.mp4";

    setStatus(
      `目標 ${targetMb} MB · ${duration.toFixed(1)}s · 映像 約 ${Math.round(videoBps / 1000)} kbps`,
    );

    await encodeOnce(ff, selectedFile, outName, videoBps, codec, 0.35, 0.9, "圧縮中…");

    let data = await ff.readFile(outName);
    let size = data.byteLength;

    if (size > targetBytes) {
      setStatus(`少し超過（${formatMB(size)} MB）。再圧縮しています…`);
      const ratio = (targetBytes * SIZE_MARGIN) / size;
      videoBps = Math.max(MIN_VIDEO_BPS, Math.floor(videoBps * ratio));
      try {
        await ff.deleteFile(outName);
      } catch {
        /* ignore */
      }
      await encodeOnce(ff, selectedFile, outName, videoBps, codec, 0.9, 0.99, "再圧縮中…");
      data = await ff.readFile(outName);
      size = data.byteLength;
    }

    try {
      await ff.deleteFile(outName);
    } catch {
      /* ignore */
    }

    const blob = new Blob([data.buffer], { type: "video/mp4" });
    objectUrl = URL.createObjectURL(blob);
    const base = selectedFile.name.replace(/\.[^.]+$/, "");
    const label = Number.isInteger(targetMb) ? String(targetMb) : String(targetMb).replace(".", "p");
    const downloadName = `${base}_${label}mb.mp4`;

    els.preview.src = objectUrl;
    els.downloadLink.href = objectUrl;
    els.downloadLink.download = downloadName;
    els.result.hidden = false;

    setProgress(100, "完了");
    const note =
      size > targetBytes
        ? `完了（${formatMB(size)} MB）。まだ少し超えています。上限を上げるか短い動画にしてください。`
        : `完了: ${formatMB(selectedFile.size)} MB → ${formatMB(size)} MB`;
    setStatus(note);
  } catch (err) {
    console.error(err);
    const msg = err?.message || String(err);
    setStatus(`失敗: ${msg}`);
    setProgress(0, "エラー");
  } finally {
    els.compressBtn.disabled = !selectedFile;
  }
}

function wireUi() {
  els.dropzone.addEventListener("click", () => els.fileInput.click());
  els.dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      els.fileInput.click();
    }
  });
  els.fileInput.addEventListener("change", () => selectFile(els.fileInput.files?.[0]));

  ["dragenter", "dragover"].forEach((type) => {
    els.dropzone.addEventListener(type, (e) => {
      e.preventDefault();
      els.dropzone.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    els.dropzone.addEventListener(type, (e) => {
      e.preventDefault();
      els.dropzone.classList.remove("is-dragover");
    });
  });
  els.dropzone.addEventListener("drop", (e) => {
    selectFile(e.dataTransfer?.files?.[0]);
  });

  els.compressBtn.addEventListener("click", () => compress());
}

wireUi();
setStatus("初回はエンコーダ（約30MB）の読み込みがあります。");
