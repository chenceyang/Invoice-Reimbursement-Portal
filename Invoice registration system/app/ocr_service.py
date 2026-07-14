import os
import re
import logging
import subprocess
import tempfile
import threading
from datetime import datetime
from PyPDF2 import PdfReader
import pdfplumber

PADDLE_CACHE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".paddle_cache"))
PADDLE_HOME_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".paddle_home"))
os.makedirs(PADDLE_CACHE_DIR, exist_ok=True)
os.makedirs(PADDLE_HOME_DIR, exist_ok=True)
os.environ.setdefault("PADDLE_PDX_CACHE_HOME", PADDLE_CACHE_DIR)
os.environ["HOME"] = PADDLE_HOME_DIR
os.environ["USERPROFILE"] = PADDLE_HOME_DIR
PADDLE_OCR_CPU_THREADS = max(
    1,
    int(os.getenv("PADDLE_OCR_CPU_THREADS", str(min(8, os.cpu_count() or 1)))),
)
os.environ["FLAGS_use_mkldnn"] = "1"
os.environ["FLAGS_use_onednn"] = "1"
os.environ["OMP_NUM_THREADS"] = str(PADDLE_OCR_CPU_THREADS)
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

PAPER_INVOICE_TYPE_ALIASES = {
    "出租车发票": ["出租车发票", "出租车", "打车", "出租"],
    "定额发票": ["定额发票", "定额"],
    "火车票": ["火车票", "火车", "铁路", "高铁", "动车"],
    "过路费": ["过路费", "过路", "高速", "通行费"],
}

_PADDLE_OCR = None
_PADDLE_OCR_AVAILABLE = None
_PADDLE_OCR_INIT_LOCK = threading.Lock()
_PADDLE_OCR_INFERENCE_LOCK = threading.Lock()
logger = logging.getLogger(__name__)
PADDLE_OCR_MAX_IMAGE_SIZE = (640, 640)
PADDLE_OCR_ENABLE_EXTRA_PASS = os.getenv("PADDLE_OCR_ENABLE_EXTRA_PASS", "0").lower() in {
    "1", "true", "yes", "on"
}
PADDLE_OCR_MOBILE_MODELS = {
    # PaddleOCR 的 mobile 系列使用轻量级主干，避免加载 ResNet/server 模型。
    "text_detection_model_name": "PP-OCRv5_mobile_det",
    "text_recognition_model_name": "PP-OCRv5_mobile_rec",
}
PADDLE_OCR_INT8_MODEL_ROOT = os.path.abspath(
    os.getenv(
        "PADDLE_OCR_INT8_MODEL_ROOT",
        os.path.join(PADDLE_CACHE_DIR, "quantized_models"),
    )
)


def _normalize_date(s: str) -> str:
    """把 2026年7月7日 / 2026-7-7 / 2026/7/7 统一成 YYYY-MM-DD"""
    if not s:
        return ""
    s = str(s).strip()
    m_short = re.search(r"(\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", s)
    if m_short:
        y, mo, d = m_short.groups()
        value = f"20{int(y):02d}-{int(mo):02d}-{int(d):02d}"
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return ""
    s = s.replace("年", "-").replace("月", "-").replace("日", "")
    s = s.replace("/", "-").replace(".", "-").replace(" ", "")
    m = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
        value = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return ""
    return ""


def _clean_text(text: str) -> str:
    """基础清洗：统一换行、去掉过多空白"""
    if not text:
        return ""
    text = text.replace("\r", "\n")
    # 把连续空格压缩一下，但保留换行
    text = re.sub(r"[ \t]+", " ", text)
    # 多个空行压缩
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _compact_label_text(text: str) -> str:
    """兼容 PDF 抽取时把标签拆成“发 票 号 码”的情况。"""
    replacements = [
        ("发 票 号 码", "发票号码"),
        ("发 票 代 码", "发票代码"),
        ("开 票 日 期", "开票日期"),
        ("价 税 合 计", "价税合计"),
        ("购 买 方 信 息", "购买方信息"),
        ("销 售 方 信 息", "销售方信息"),
        ("项 目 名 称", "项目名称"),
        ("小 写", "小写"),
        ("名 称", "名称"),
        ("通 行 费", "通行费"),
        ("金 额", "金额"),
        ("日 期", "日期"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    # 部分电子发票把左右两侧的竖排栏标题抽成逐字换行，例如：
    # “购\n买\n方\n名\n称”。统一成区块标题，后续即可按购买方/销售方
    # 分别提取名称和税号。
    text = re.sub(r"购\s*买\s*方\s*(?:信\s*息|名\s*称)", "购买方信息", text)
    text = re.sub(r"销\s*售\s*方\s*(?:信\s*息|名\s*称)", "销售方信息", text)
    return text


def _flatten_paddle_result(result) -> str:
    texts = []

    def walk(node):
        if node is None:
            return
        if isinstance(node, dict):
            for key in ("rec_texts", "texts"):
                value = node.get(key)
                if isinstance(value, list):
                    texts.extend(str(item) for item in value if item)
            for key in ("text", "label"):
                value = node.get(key)
                if isinstance(value, str) and value:
                    texts.append(value)
            for value in node.values():
                if isinstance(value, (list, tuple, dict)):
                    walk(value)
            return
        if isinstance(node, (list, tuple)):
            if len(node) >= 2 and isinstance(node[1], (list, tuple)) and node[1]:
                if isinstance(node[1][0], str):
                    texts.append(node[1][0])
                    return
            for item in node:
                walk(item)

    walk(result)
    return _compact_label_text(_clean_text("\n".join(texts)))


def _get_paddle_ocr():
    global _PADDLE_OCR, _PADDLE_OCR_AVAILABLE
    if _PADDLE_OCR_AVAILABLE is False:
        return None
    if _PADDLE_OCR is not None:
        return _PADDLE_OCR

    with _PADDLE_OCR_INIT_LOCK:
        # 后台预热和首个请求可能同时到达，进入锁后必须再次检查状态。
        if _PADDLE_OCR_AVAILABLE is False:
            return None
        if _PADDLE_OCR is not None:
            return _PADDLE_OCR

        try:
            from paddleocr import PaddleOCR
        except Exception:
            logger.exception("PaddleOCR import failed")
            _PADDLE_OCR_AVAILABLE = False
            return None

        model_options = PADDLE_OCR_MOBILE_MODELS
        cpu_options = {
            "device": "cpu",
            "enable_mkldnn": True,
            "cpu_threads": PADDLE_OCR_CPU_THREADS,
            "text_det_limit_side_len": PADDLE_OCR_MAX_IMAGE_SIZE[0],
            "text_det_limit_type": "max",
        }
        int8_det_dir = os.path.join(PADDLE_OCR_INT8_MODEL_ROOT, "PP-OCRv5_mobile_det_int8")
        int8_rec_dir = os.path.join(PADDLE_OCR_INT8_MODEL_ROOT, "PP-OCRv5_mobile_rec_int8")
        if _is_paddle_inference_model(int8_det_dir) and _is_paddle_inference_model(int8_rec_dir):
            model_options = {
                "text_detection_model_dir": int8_det_dir,
                "text_recognition_model_dir": int8_rec_dir,
            }

        # 每个兼容分支都显式指定 mobile 模型，初始化失败时也不会退回较重的
        # ResNet/server 默认模型。快速模式关闭方向分类以减少推理耗时。
        init_attempts = [
            {
                **model_options,
                **cpu_options,
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
            },
            {
                **model_options,
                **cpu_options,
                "use_textline_orientation": False,
            },
        ]
        for kwargs in init_attempts:
            try:
                _PADDLE_OCR = PaddleOCR(**kwargs)
                _PADDLE_OCR_AVAILABLE = True
                return _PADDLE_OCR
            except TypeError as exc:
                logger.debug("PaddleOCR init compatibility attempt failed: %s", exc)
            except Exception:
                logger.exception("PaddleOCR initialization attempt failed")

        _PADDLE_OCR_AVAILABLE = False
        return None


def _is_paddle_inference_model(model_dir: str) -> bool:
    """只有完整的量化模型存在时才允许切换，避免半成品导致 OCR 不可用。"""
    if not os.path.isdir(model_dir):
        return False
    has_program = any(
        os.path.isfile(os.path.join(model_dir, filename))
        for filename in ("inference.json", "inference.pdmodel", "model.pdmodel")
    )
    has_params = any(
        os.path.isfile(os.path.join(model_dir, filename))
        for filename in ("inference.pdiparams", "model.pdiparams")
    )
    return has_program and has_params


def _prepare_paddle_input(path: str):
    """将大图等比例缩小到模型所需范围，返回推理路径和待清理的临时文件。"""
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            if image.width <= PADDLE_OCR_MAX_IMAGE_SIZE[0] and image.height <= PADDLE_OCR_MAX_IMAGE_SIZE[1]:
                return path, ""

            image.thumbnail(PADDLE_OCR_MAX_IMAGE_SIZE, Image.Resampling.LANCZOS)
            if image.mode not in {"L", "RGB"}:
                image = image.convert("RGB")

            temp = tempfile.NamedTemporaryFile(prefix="paddle_ocr_640_", suffix=".png", delete=False)
            temp.close()
            image.save(temp.name)
            return temp.name, temp.name
    except Exception:
        # 预处理失败时保留原有行为，避免单张异常图片导致识别中断。
        return path, ""


def _run_paddle_ocr(path: str) -> str:
    ocr = _get_paddle_ocr()
    if ocr is None:
        return ""

    input_path, temp_path = _prepare_paddle_input(path)
    try:
        call_attempts = [
            lambda: ocr.predict(input_path),
            lambda: ocr.ocr(input_path, cls=True),
            lambda: ocr.ocr(input_path),
        ]
        with _PADDLE_OCR_INFERENCE_LOCK:
            for call in call_attempts:
                try:
                    text = _flatten_paddle_result(call())
                    if text:
                        return text
                except TypeError as exc:
                    logger.debug("PaddleOCR call compatibility attempt failed: %s", exc)
                except Exception:
                    logger.exception("PaddleOCR inference attempt failed for %s", input_path)
        return ""
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _run_local_ocr(path: str) -> str:
    text = _run_paddle_ocr(path)
    if text:
        return text
    return _run_windows_ocr(path)


def _run_windows_ocr(path: str) -> str:
    ps_script = r"""
param([string]$ImagePath)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
function AwaitOp($op, $type) {
  $m = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1
  } | Select-Object -First 1
  $task = $m.MakeGenericMethod($type).Invoke($null, @($op))
  $task.Wait()
  $task.Result
}
$texts = New-Object System.Collections.Generic.List[string]
$file = AwaitOp ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
$stream = AwaitOp ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = AwaitOp ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = AwaitOp ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
foreach ($lang in @('zh-Hans', 'en-US')) {
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($lang))
  if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
  if ($null -ne $engine) {
    $result = AwaitOp ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    if ($result.Text) { $texts.Add($result.Text) }
  }
}
$texts -join "`n"
"""
    script_path = ""
    try:
        script = tempfile.NamedTemporaryFile(prefix="invoice_windows_ocr_", suffix=".ps1", delete=False, mode="w", encoding="utf-8")
        script.write(ps_script)
        script.close()
        script_path = script.name
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path, path],
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="ignore",
        )
    except Exception:
        return ""
    finally:
        if script_path:
            try:
                os.remove(script_path)
            except Exception:
                pass
    if result.returncode != 0:
        return ""
    return result.stdout or ""


def _make_ocr_variants(path: str) -> list:
    try:
        from PIL import Image, ImageEnhance, ImageFilter
    except Exception:
        return [path]

    variants = [path]
    temp_paths = []
    try:
        img = Image.open(path)
        base = img.convert("L")
        scale = 2 if max(base.size) < 2200 else 1
        if scale > 1:
            base = base.resize((base.width * scale, base.height * scale))
        enhanced = ImageEnhance.Contrast(base).enhance(1.8).filter(ImageFilter.SHARPEN)

        crops = [
            enhanced,
            enhanced.crop((0, 0, enhanced.width, int(enhanced.height * 0.55))),
            enhanced.crop((0, int(enhanced.height * 0.45), enhanced.width, enhanced.height)),
        ]
        for image in crops:
            temp = tempfile.NamedTemporaryFile(prefix="invoice_ocr_", suffix=".png", delete=False)
            temp.close()
            image.save(temp.name)
            temp_paths.append(temp.name)
    except Exception:
        return variants
    return variants + temp_paths


def _extract_image_text(path: str) -> str:
    """
    图片发票优先使用本地 PaddleOCR；不可用时才走旧的系统 OCR 兜底。
    """
    paddle_text = _run_paddle_ocr(path)
    if paddle_text:
        return paddle_text

    texts = []
    temp_paths = []
    variants = _make_ocr_variants(path)
    temp_paths = [p for p in variants if p != path]

    for variant in variants:
        win_text = _run_windows_ocr(variant)
        if win_text:
            texts.append(win_text)

    try:
        from PIL import Image
        import pytesseract
    except Exception:
        for temp_path in temp_paths:
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return _compact_label_text(_clean_text("\n".join(texts)))

    for tesseract_path in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ):
        if os.path.exists(tesseract_path):
            try:
                pytesseract.pytesseract.tesseract_cmd = tesseract_path
            except Exception:
                pass
            break

    for variant in variants:
        try:
            image = Image.open(variant)
        except Exception:
            continue
        for config in ("--psm 6", "--psm 11"):
            try:
                texts.append(pytesseract.image_to_string(image, lang="chi_sim+eng", config=config))
            except Exception:
                try:
                    texts.append(pytesseract.image_to_string(image, lang="eng", config=config))
                except Exception:
                    continue

    for temp_path in temp_paths:
        try:
            os.remove(temp_path)
        except Exception:
            pass

    return _compact_label_text(_clean_text("\n".join(texts)))


def _extract_digits_from_image_filename(path: str, invoice_type_hint: str) -> dict:
    try:
        from PIL import Image
        from PIL import ImageEnhance, ImageFilter
    except Exception:
        return {}

    try:
        img = Image.open(path)
    except Exception:
        return {}

    info = {}
    # 火车票底部编号区域 OCR 容易漏字，单独裁剪放大再识别一次。
    if invoice_type_hint == "火车票":
        try:
            bottom = img.crop((0, int(img.height * 0.76), img.width, img.height))
            bottom = bottom.resize((bottom.width * 3, bottom.height * 3))
            temp = tempfile.NamedTemporaryFile(prefix="train_code_", suffix=".png", delete=False)
            temp.close()
            bottom.save(temp.name)
            text = _run_local_ocr(temp.name)
            os.remove(temp.name)
            code = _extract_train_invoice_no(text)
            if code:
                info["invoice_no"] = code
        except Exception:
            pass

    if invoice_type_hint == "出租车发票":
        try:
            code_area = img.crop((0, int(img.height * 0.06), img.width, int(img.height * 0.23))).convert("RGB")
            code_area = code_area.resize((code_area.width * 6, code_area.height * 6))
            dark_mask = Image.new("L", code_area.size, 255)
            source = code_area.load()
            target = dark_mask.load()
            for y in range(code_area.height):
                for x in range(code_area.width):
                    r, g, b = source[x, y]
                    # 保留棕黑色发票代码，尽量压掉红章和浅色底纹。
                    if r < 185 and g < 175 and b < 160 and not (r > 155 and g < 95 and b < 130):
                        target[x, y] = 0
            variants = [
                dark_mask,
                ImageEnhance.Contrast(code_area.convert("L")).enhance(4).filter(ImageFilter.SHARPEN),
                dark_mask.filter(ImageFilter.MedianFilter(3)),
            ]
            texts = []
            for image in variants:
                temp = tempfile.NamedTemporaryFile(prefix="taxi_code_", suffix=".png", delete=False)
                temp.close()
                image.save(temp.name)
                texts.append(_run_local_ocr(temp.name))
                os.remove(temp.name)
            code = _extract_taxi_invoice_code_from_loose_text("\n".join(texts))
            if code:
                info["invoice_no"] = code
        except Exception:
            pass

    if invoice_type_hint == "过路费":
        try:
            fee_area = img.crop((0, int(img.height * 0.45), img.width, int(img.height * 0.75))).convert("L")
            fee_area = fee_area.resize((fee_area.width * 5, fee_area.height * 5))
            variants = [
                fee_area,
                ImageEnhance.Contrast(fee_area).enhance(4).filter(ImageFilter.SHARPEN),
                fee_area.point(lambda p: 255 if p > 180 else 0),
            ]
            texts = []
            for image in variants:
                temp = tempfile.NamedTemporaryFile(prefix="toll_amount_", suffix=".png", delete=False)
                temp.close()
                image.save(temp.name)
                texts.append(_run_local_ocr(temp.name))
                os.remove(temp.name)
            amount = _extract_toll_amount_from_loose_text("\n".join(texts))
            if amount:
                info["amount"] = amount
        except Exception:
            pass

    if invoice_type_hint == "定额发票":
        try:
            code_area = img.crop((int(img.width * 0.15), int(img.height * 0.30), int(img.width * 0.72), int(img.height * 0.52))).convert("L")
            code_area = code_area.resize((code_area.width * 5, code_area.height * 5))
            variants = [
                code_area,
                ImageEnhance.Contrast(code_area).enhance(4).filter(ImageFilter.SHARPEN),
                code_area.point(lambda p: 255 if p > 170 else 0),
            ]
            texts = []
            for image in variants:
                temp = tempfile.NamedTemporaryFile(prefix="fixed_code_", suffix=".png", delete=False)
                temp.close()
                image.save(temp.name)
                texts.append(_run_local_ocr(temp.name))
                os.remove(temp.name)
            code = _extract_fixed_invoice_code_from_loose_text("\n".join(texts))
            if code:
                info["invoice_no"] = code
        except Exception:
            pass

    return info


def _extract_taxi_invoice_code_from_loose_text(text: str) -> str:
    compact = _strip_all_spaces(text).upper()
    compact = compact.replace("Ｏ", "0").replace("O", "0")
    compact = compact.replace("Ｉ", "1").replace("I", "1").replace("上", "1").replace("丨", "1")
    compact = compact.replace("Ｚ", "2").replace("Z", "2")
    exact = re.findall(r"([0-9]{8}A[0-9]{3})", compact)
    if exact:
        return exact[0]
    candidates = re.findall(r"([0-9]{6,10}A[0-9]{3,4})", compact)
    if candidates:
        candidate = sorted(candidates, key=len, reverse=True)[0]
        m = re.search(r"([0-9]{8}A[0-9]{3})", candidate)
        if m:
            return m.group(1)
        return candidate
    return ""


def _extract_fixed_invoice_code_from_loose_text(text: str) -> str:
    text = _strip_all_spaces(text).upper()
    text = text.replace("Ｏ", "0").replace("O", "0")
    text = text.replace("Ｆ", "F").replace("％", "").replace("%", "")
    text = text.replace("上", "1").replace("丨", "1").replace("一", "1")
    candidates = re.findall(r"([0-9]{8}E[0-9]{3})", text)
    if candidates:
        return candidates[0]
    m = re.search(r"([0-9]{8}E[0-9]{1,3})", text)
    if m:
        return m.group(1).ljust(12, "1")
    return ""


def _extract_toll_amount_from_loose_text(text: str) -> str:
    text = _normalize_ocr_text(text)
    candidates = []
    for raw in re.findall(r"([1-9][0-9]{0,3})\s*元", text):
        try:
            value = int(raw)
        except ValueError:
            continue
        if 1 <= value <= 1000 and value not in {202, 2020, 2026}:
            candidates.append(value)
    if not candidates:
        return ""
    return f"{candidates[-1]:.2f}"


def _extract_text(path: str) -> str:
    """
    PDF 走文本抽取；图片走可选 OCR。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        texts = []
        try:
            reader = PdfReader(path)
            for page in reader.pages:
                try:
                    texts.append(page.extract_text() or "")
                except Exception:
                    continue
        except Exception:
            pass

        try:
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    try:
                        texts.append(page.extract_text(x_tolerance=2, y_tolerance=3) or "")
                    except Exception:
                        continue
        except Exception:
            pass

        return _compact_label_text(_clean_text("\n".join(texts)))
    if ext in {".jpg", ".jpeg", ".png"}:
        return _extract_image_text(path)
    return ""


def _search(patterns, text, flags=re.S):
    """按多个 pattern 依次匹配，返回第一个 group(1)"""
    if isinstance(patterns, str):
        patterns = [patterns]
    for p in patterns:
        m = re.search(p, text, flags)
        if m:
            return m.group(1).strip()
    return ""


def _squash_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _strip_all_spaces(text: str) -> str:
    return re.sub(r"\s+", "", (text or ""))


def _collapse_cjk_name_spacing(text: str) -> str:
    """移除 PDF 文本层在连续中文字符之间插入的排版空格。"""
    value = _squash_spaces(text)
    return re.sub(r"(?<=[\u3400-\u4dbf\u4e00-\u9fff])\s+(?=[\u3400-\u4dbf\u4e00-\u9fff])", "", value)


def _clean_amount(value: str) -> str:
    value = (value or "")
    value = value.replace("飢", "0").replace("〇", "0").replace("Ｏ", "0").replace("O", "0").replace("o", "0")
    value = value.replace("，", ".").replace("。", ".").replace("．", ".").replace("·", ".")
    value = value.replace(",", "").replace("¥", "").replace("￥", "").replace("元", "").strip()
    m = re.search(r"\d+(?:\.\d+)?", value)
    if not m:
        return ""
    amount = m.group(0)
    if "." not in amount and len(amount) >= 3:
        amount = amount[:-2] + "." + amount[-2:]
    return f"{float(amount):.2f}"


def _valid_name(value: str) -> str:
    value = _collapse_cjk_name_spacing(value)
    value = value.strip(" ：:")
    if not value or value in {"名称", "名称："}:
        return ""
    return value


def _detect_paper_invoice_type_from_filename(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    normalized = stem.replace(" ", "").replace("_", "").replace("-", "")
    for type_name, aliases in PAPER_INVOICE_TYPE_ALIASES.items():
        if any(alias in normalized for alias in aliases):
            return type_name
    return ""


def _is_image_file(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() in {".jpg", ".jpeg", ".png"}


def _make_orientation_paths(path: str) -> list:
    if not _is_image_file(path):
        return []

    try:
        from PIL import Image, ImageOps
    except Exception:
        return []

    paths = []
    try:
        img = ImageOps.exif_transpose(Image.open(path))
        rotations = [270, 90, 180]
        for angle in rotations:
            rotated = img.rotate(angle, expand=True)
            temp = tempfile.NamedTemporaryFile(prefix=f"invoice_oriented_{angle}_", suffix=".png", delete=False)
            temp.close()
            rotated.save(temp.name)
            paths.append(temp.name)
    except Exception:
        return []
    return paths


def _cleanup_temp_paths(paths: list, original_path: str):
    for path in paths:
        if path == original_path:
            continue
        try:
            os.remove(path)
        except Exception:
            pass


def _invoice_info_score(info: dict, invoice_type_hint: str = "") -> int:
    score = 0
    invoice_no = info.get("invoice_no") or ""
    amount = info.get("amount") or ""
    seller_name = info.get("seller_name") or ""
    invoice_date = info.get("invoice_date") or ""

    if invoice_no:
        score += 12
        score += min(len(invoice_no), 20)
    if amount:
        score += 20
    if seller_name:
        score += 12 + min(len(seller_name), 20)
    if invoice_date:
        score += 12
    if info.get("project_name"):
        score += 4

    if invoice_type_hint == "出租车发票":
        if re.fullmatch(r"\d{8}A\d{3}", invoice_no):
            score += 40
        if seller_name.endswith("有限公司"):
            score += 12
    elif invoice_type_hint == "定额发票":
        if re.fullmatch(r"\d{8}E\d{3}", invoice_no):
            score += 40
        if amount:
            score += 20
    elif invoice_type_hint == "过路费":
        if re.fullmatch(r"\d{12}", invoice_no):
            score += 30
        if "高速公路" in seller_name:
            score += 25
    elif invoice_type_hint == "火车票":
        if "JM" not in invoice_no and re.search(r"\d{10,}", invoice_no):
            score += 20
        if amount and invoice_date:
            score += 15

    return score


def _is_good_invoice_info(info: dict, invoice_type_hint: str = "") -> bool:
    invoice_no = info.get("invoice_no") or ""
    amount = info.get("amount") or ""
    seller_name = info.get("seller_name") or ""
    invoice_date = info.get("invoice_date") or ""

    if invoice_type_hint == "出租车发票":
        return bool(re.fullmatch(r"\d{8}A\d{3}", invoice_no) and amount and seller_name)
    if invoice_type_hint == "定额发票":
        return bool(re.fullmatch(r"\d{8}E\d{3}", invoice_no) and amount)
    if invoice_type_hint == "过路费":
        return bool(re.fullmatch(r"\d{12}", invoice_no) and amount and seller_name)
    if invoice_type_hint == "火车票":
        return bool(invoice_no and amount and invoice_date)
    return _invoice_info_score(info, invoice_type_hint) >= 80


def _chinese_amount_to_number(text: str) -> str:
    text = _strip_all_spaces(text)
    if not text:
        return ""

    digit_map = {
        "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "贰": 2, "两": 2,
        "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5,
        "六": 6, "陆": 6, "七": 7, "柒": 7, "八": 8, "捌": 8,
        "九": 9, "玖": 9,
    }
    unit_map = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
    section_unit_map = {"万": 10000, "萬": 10000, "亿": 100000000, "億": 100000000}

    integer_part = re.split(r"[元圆块]", text)[0]
    if not integer_part:
        return ""

    total = 0
    section = 0
    number = 0
    found = False
    for char in integer_part:
        if char in digit_map:
            number = digit_map[char]
            found = True
        elif char in unit_map:
            unit = unit_map[char]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
            found = True
        elif char in section_unit_map:
            section += number
            total += section * section_unit_map[char]
            section = 0
            number = 0
            found = True

    amount = total + section + number
    if not found:
        return ""
    return f"{amount:.2f}"


def _normalize_ocr_text(text: str) -> str:
    text = (text or "").replace("￥", "¥")
    text = text.replace("：", ":")
    text = text.replace("·", ".").replace("。", ".").replace("．", ".")
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _extract_line_after_label(text: str, label_pattern: str, max_chars: int = 80) -> str:
    m = re.search(label_pattern + r"\s*[:：]?\s*([^\n]{1," + str(max_chars) + r"})", text, re.S)
    if m:
        return m.group(1).strip()
    return ""


def _extract_invoice_code(text: str) -> str:
    compact = _strip_all_spaces(text).upper()
    compact = compact.replace("Ｏ", "0").replace("O", "0").replace("Ｉ", "1").replace("I", "1")
    compact = compact.replace("上", "1").replace("丨", "1").replace("哆", "2")
    m = re.search(r"发票代码([0-9A-Z]{8,24})", compact, re.I)
    if m:
        code = m.group(1)
        # 防止把后面的“发票号码”一起吞进去。
        code = re.split(r"发票|号码", code)[0]
        if len(code) >= 8:
            return code[:14]

    patterns = [
        r"发票代码\s*[:：]?\s*([0-9A-Z ]{8,30})",
        r"发票代\s*码\s*[:：]?\s*([0-9A-Z ]{8,30})",
    ]
    return _search(patterns, text, flags=re.S | re.I).replace(" ", "").upper()


def _extract_toll_invoice_code(text: str) -> str:
    compact = _strip_all_spaces(text).upper()
    compact = compact.replace("Ｏ", "0").replace("O", "0").replace("Ｉ", "1").replace("I", "1")
    m = re.search(r"发票代码([0-9]{12})", compact)
    if m:
        return m.group(1)
    code = _extract_invoice_code(text)
    digits = re.sub(r"\D", "", code)
    return digits[:12] if len(digits) >= 12 else ""


def _extract_train_invoice_no(text: str) -> str:
    compact = _strip_all_spaces(text).upper()
    compact = compact.replace("０", "0").replace("Ｏ", "0").replace("O", "0")
    compact = compact.replace("Ｉ", "1").replace("I", "1").replace("Ｌ", "1")
    compact = compact.replace("ＪＭ", "JM")
    candidates = []
    for m in re.finditer(r"(.{8,80})J[MＮN]", compact, re.S | re.I):
        code = re.sub(r"[^0-9A-Z]", "", m.group(1).upper())
        code = re.sub(r"^[A-Z]+(?=\d)", "", code)
        if len(code) >= 14 and re.search(r"\d{10,}", code):
            candidates.append(code[-20:] if len(code) > 20 else code)
    if not candidates:
        return ""
    return sorted(candidates, key=lambda item: (len(item), sum(ch.isdigit() for ch in item)), reverse=True)[0]


def _extract_train_date(text: str) -> str:
    normalized = _normalize_ocr_text(text)
    m = re.search(r"(\d{2,4})\s*年\s*(\d{1,2})\s*月\s*([0-9一二三四五六七八九十私旧]{1,3})\s*日", normalized)
    if not m:
        return _normalize_date(_search([
            r"(20\d{2}年\d{1,2}月\d{1,2}日)",
            r"(\d{2}年\d{1,2}月\d{1,2}日)",
            r"(20\d{2}[-/\.]\d{1,2}[-/\.]\d{1,2})",
        ], normalized))

    y, mo, day = m.groups()
    if len(y) == 2:
        y = "20" + y
    day_map = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        "私": 14, "旧": 14,
    }
    if day.isdigit():
        day_num = int(day)
    elif day in day_map:
        day_num = day_map[day]
    elif day.startswith("十") and len(day) == 2 and day[1] in day_map:
        day_num = 10 + day_map[day[1]]
    else:
        return ""
    return f"{int(y):04d}-{int(mo):02d}-{day_num:02d}"


def _extract_train_amount(text: str) -> str:
    normalized = _normalize_ocr_text(text)
    patterns = [
        r"[¥￥Y]\s*([0-9 ]+[.·。．]\s*[0-9])\s*元",
        r"[¥￥Y]\s*([0-9 ]+(?:[.·。．]\s*[0-9])?)",
    ]
    raw = _search(patterns, normalized, flags=re.S | re.I)
    raw = re.sub(r"\s+", "", raw).replace("·", ".").replace("。", ".").replace("．", ".")
    if raw and "." not in raw and len(raw) > 2:
        raw = raw[:-1] + "." + raw[-1:]
    return _clean_amount(raw)


def _extract_paper_seller_after_invoice_no(text: str) -> str:
    lines = [_squash_spaces(line) for line in text.splitlines()]
    for idx, line in enumerate(lines):
        if re.search(r"发票号\s*码|发票号码", line):
            for next_line in lines[idx + 1:idx + 5]:
                if re.search(r"[\u4e00-\u9fff]{4,}", next_line) and not re.search(r"电话|监督|日期|金额|上车|下车", next_line):
                    return _valid_name(next_line)
    return ""


def _clean_paper_company(value: str) -> str:
    value = _squash_spaces(value)
    value = re.sub(r"^(?:发票号码|发票代码|号码|代码)+", "", value)
    value = re.sub(r"[0-9A-Za-z:：'`·。。，,<>《》()\[\]{}|/\\]+", " ", value)
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"^(?:月印|印刷|发四川着视联|四川发发票)+", "", value)
    m = re.search(r"([\u4e00-\u9fff]{2,}(?:有限公司|分公司))", value)
    if m:
        company = m.group(1)
        company = re.sub(r"^(?:月印|印刷)+", "", company)
        return company
    return value if len(value) >= 4 else ""


def _extract_company_between_invoice_no_and_next_label(text: str) -> str:
    compact = _strip_all_spaces(text)
    search_area = compact
    idx = compact.find("发票号码")
    if idx >= 0:
        search_area = compact[idx:idx + 260]

    candidates = []
    for m in re.finditer(r"([\u4e00-\u9fff]{2,}(?:有限公司|分公司))", search_area):
        company = _clean_paper_company(m.group(1))
        if company and not re.search(r"印刷|票证印务|国家税务", company):
            candidates.append(company)
    if candidates:
        return sorted(candidates, key=lambda item: (len(item), "萨" not in item), reverse=True)[0]

    m = re.search(r"发票号码[\s0-9A-Z]*([\s\S]{0,120}?)(?:监督电话|入站|口站|出站|车类|通行费|亍费|公司电话|日期|$)", text, re.I)
    if m:
        return _clean_paper_company(m.group(1))
    return ""


def _extract_taxi_amount(text: str) -> str:
    normalized = _normalize_ocr_text(text)
    amount = _clean_amount(_search([
        r"[¥￥]\s*([1-9][0-9]*(?:[.·。．，,][0-9]{1,2})?)\s*(?:金额|金\s*额)",
        r"(?:金额|金\s*额)[\s\S]{0,30}?[¥￥]\s*([1-9][0-9]*(?:[.·。．，,][0-9]{1,2})?)",
    ], normalized, flags=re.S | re.I))
    if amount and float(amount) > 0:
        return amount

    compact = _strip_all_spaces(normalized)
    idx = compact.find("金额")
    if idx >= 0:
        window = compact[idx:idx + 160]
        m = re.search(r"([1-9][0飢O〇]{1,2}[.·。．，,]?[0飢O〇]{2})", window, re.I)
        if m:
            amount = _clean_amount(m.group(1))
            if amount and float(amount) > 0:
                return amount

    candidates = [
        _search([
            r"金额\s*[:：；;]?\s*[¥￥]?\s*([0-9O〇飢 ]{1,8}[.·。．，,]?\s*[0-9O〇飢]{0,2})",
            r"金额[\s\S]{0,40}?([0-9O〇飢]\s*[0-9O〇飢]?\s*[.·。．，,]\s*[0-9O〇飢]{2})",
            r"金额[\s\S]{0,80}?([0-9O〇飢]{1,3})\s*[飢O〇0]{1,2}",
        ], normalized, flags=re.S | re.I),
    ]
    for candidate in candidates:
        amount = _clean_amount(candidate)
        if amount and float(amount) > 0:
            return amount
    return ""


def _extract_toll_amount(text: str) -> str:
    normalized = _normalize_ocr_text(text)
    amount = _clean_amount(_search([
        r"通行费\s*[¥￥]\s*[:：；;]?\s*([1-9][0-9O〇飢]*(?:[.·。．，,][0-9O〇飢]{1,2})?)",
        r"通\s*行\s*费\s*[¥￥]\s*[:：；;]?\s*([1-9][0-9O〇飢]*(?:[.·。．，,][0-9O〇飢]{1,2})?)",
        r"亍\s*费\s*[¥￥]\s*[:：；;]?\s*([1-9][0-9O〇飢]*(?:[.·。．，,][0-9O〇飢]{1,2})?)",
        r"([0-9O〇飢]+(?:[.·。．，,][0-9O〇飢]{1,2})?)\s*元[\s\S]{0,40}?通行费",
    ], normalized, flags=re.S | re.I))
    if amount and float(amount) > 0:
        return amount

    upper_amount = _search([
        r"金额大写\s*[:：]?\s*([零〇一壹二贰两三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟万萬亿億元圆块整\s]+)",
    ], normalized, flags=re.S)
    return _chinese_amount_to_number(upper_amount)


def _extract_paper_invoice_info(text: str, invoice_type_hint: str = "") -> dict:
    normalized = _normalize_ocr_text(text)
    compact = _strip_all_spaces(normalized)
    info = {
        "invoice_date": "",
        "invoice_no": "",
        "seller_name": "",
        "amount": "",
        "project_name": "",
    }

    if invoice_type_hint == "出租车发票":
        info["invoice_no"] = _extract_taxi_invoice_code_from_loose_text(normalized) or _extract_invoice_code(normalized)
        info["invoice_date"] = _normalize_date(_search([
            r"日期\s*[:：]?\s*(20\d{2}[-年/\.]\d{1,2}[-月/\.]\d{1,2}日?)",
            r"日期[\s\S]{0,30}?(\d{4}[-年/\.]\d{1,2}[-月/\.]\d{1,2}日?)",
            r"(20\d{2}[-年/\.]\d{1,2}[-月/\.]\d{1,2}日?)",
        ], normalized))
        info["amount"] = _extract_taxi_amount(normalized)
        seller_name = (
            _extract_company_between_invoice_no_and_next_label(normalized)
            or _extract_paper_seller_after_invoice_no(normalized)
        )
        info["seller_name"] = seller_name if seller_name.endswith("有限公司") else ""
        info["project_name"] = "出租车费"

    elif invoice_type_hint == "定额发票":
        info["invoice_no"] = _extract_invoice_code(normalized)
        upper_amount = _search([
            r"([零〇一壹二贰两三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟万萬亿億元圆块]+整)",
            r"([零〇一壹二贰两三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟万萬亿億元圆块]{2,})",
        ], compact)
        info["amount"] = _chinese_amount_to_number(upper_amount)
        info["project_name"] = "定额发票"

    elif invoice_type_hint == "过路费":
        info["invoice_no"] = _extract_toll_invoice_code(normalized)
        info["seller_name"] = (
            _extract_company_between_invoice_no_and_next_label(normalized)
            or _extract_paper_seller_after_invoice_no(normalized)
        )
        info["amount"] = _extract_toll_amount(normalized)
        info["invoice_date"] = _normalize_date(_search([
            r"(20\d{2}[-年/\.]\d{1,2}[-月/\.]\d{1,2}日?)",
        ], normalized))
        info["project_name"] = "过路费"

    elif invoice_type_hint == "火车票":
        info["amount"] = _extract_train_amount(normalized)
        info["invoice_no"] = _extract_train_invoice_no(compact)
        info["invoice_date"] = _extract_train_date(normalized)
        info["project_name"] = "火车票"

    return {k: v for k, v in info.items() if v}


def _clean_project_value(value: str, text: str = "") -> str:
    value = _squash_spaces(value)
    value = re.sub(r"\s+(?:桌|份|次|个|项|批|件|升)$", "", value).strip()
    if value.count("(") > value.count(")"):
        m = re.search(re.escape(value) + r"\s*\n\s*([A-Za-z0-9#ⅥⅦⅧ]+\))", text)
        if not m:
            m = re.search(r"\n\s*([A-Za-z0-9#ⅥⅦⅧ]{1,12}\))", text)
        if m:
            value = f"{value}{m.group(1)}"
    return value


def _project_name_part(line: str) -> str:
    """从项目明细行中截出名称部分，排除规格、单位、数量等后续列。"""
    value = _squash_spaces(line)
    value = re.split(r"\s*无\s+无(?=\s|\d|$)", value, maxsplit=1)[0]
    value = re.split(
        r"\s+(?=[+-]?\d+(?:\.\d+)?%|(?:\d+(?:\.\d+)?\s+){2,}|\d+(?:\.\d+)?\s+\d+(?:\.\d+)?%|[¥￥]\s*\d|/?\s*(?:桌|份|次|个|项|批|件|升)\s+\d)",
        value,
        maxsplit=1,
    )[0]
    return value.strip()


def _extract_invoice_date(text: str) -> str:
    # 先抓“开票日期/票据日期”
    val = _search([
        # 兼容 PDF 文本层在年月日之间插入空格：2026年 06月 29日。
        r"(?:开票日期|票据日期)\s*[:：]?\s*(20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)",
        r"(?:开票日期|票据日期)\s*[:：]?\s*(20\d{2}[年\-/\. ]\d{1,2}[月\-/\. ]\d{1,2}日?)",
        r"(?:开票日期|票据日期)\s*[:：]?\s*(20\d{2}\s+\d{1,2}\s+\d{1,2})",
        # 兼容“日期：2026-07-07”这类
        r"(?:日期)\s*[:：]?\s*(20\d{2}[年\-/\. ]\d{1,2}[月\-/\. ]\d{1,2}日?)",
        r"(20\d{2}[年\-/\. ]\d{1,2}[月\-/\. ]\d{1,2}日?)",
    ], text)
    return _normalize_date(val)


def _extract_invoice_no(text: str) -> str:
    # 常见发票号码是 8 / 20 位，但我这里放宽一点
    return _search([
        r"(?:发票号码|发票号|票号|号码|No\.?)\s*[:：]?\s*([0-9 ]{8,30})",
        # 有些 PDF 里 “发票号码” 和号码之间会换行
        r"发票号码\s*\n?\s*([0-9 ]{8,30})",
    ], text)


def _extract_amount(text: str) -> str:
    """
    优先取 价税合计(小写) / 小写 右边金额
    """
    patterns = [
        # 完整文本里常见格式：价税合计（大写） 壹佰圆整 （小写）¥100.00
        r"价税合计[^\n]{0,80}?（\s*小写\s*）\s*[¥￥]?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
        r"价税合计[^\n]{0,80}?\(\s*小写\s*\)\s*[¥￥]?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
        r"[（(]\s*小写\s*[）)]\s*[¥￥]?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
        # 价税合计(小写) ¥350.00
        r"价税合计(?:（小写）|\(小写\)|小写)?[^\n]{0,40}?[¥￥]\s*([0-9,]+(?:\.[0-9]{1,2})?)",
        # 小写 ¥350.00
        r"小写[^\n]{0,20}?[¥￥]\s*([0-9,]+(?:\.[0-9]{1,2})?)",
        # 兼容“（小写）350.00”
        r"（小写）\s*[¥￥]?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
        r"(?:合计|金额)[^\n]{0,20}?[¥￥]\s*([0-9,]+(?:\.[0-9]{1,2})?)",
    ]
    val = _search(patterns, text, flags=re.S | re.I)
    return val.replace(",", "")


def _extract_buyer_name(text: str) -> str:
    """
    兼容这类成品油电子发票：
    购 买 方 信 息
    名称：成都英普瑞生通讯设备有限公司
    """
    direct_patterns = [
        r"(?:^|\n)\s*(?:买|购)[ \t]+名称\s*[:：]\s*(.+?)[ \t]+(?:售|销)[ \t]+名称\s*[:：]",
        r"购\s+名称\s*[:：]\s*(.+?)\s+销\s+名称\s*[:：]",
        r"购\s*名称\s*[:：]\s*([^\n]+?)\s+销\s*名称\s*[:：]",
        r"购买方(?:信息)?名称\s*[:：]?\s*([^\n]+)",
        r"购\s*买\s*方\s*信\s*息名称\s*[:：]\s*([^\n]+)",
    ]
    for p in direct_patterns:
        val = _search(p, text, flags=re.S)
        val = _valid_name(val)
        if val:
            return val

    patterns = [
        # 标准一行
        r"购买方(?:信息)?名称\s*[:：]?\s*([^\n]+)",
        # 垂直标签被抽成“购\n买\n方\n信\n息\n名称：xxx”
        r"购\s*买\s*方\s*信\s*息[\s\S]{0,80}?名称\s*[:：]?\s*([^\n]+)",
        # 从购买方区块中抓名称，到销售方区块结束
        r"购\s*买\s*方\s*信\s*息([\s\S]{0,200}?)销\s*售\s*方\s*信\s*息",
    ]

    for p in patterns:
        m = re.search(p, text, re.S)
        if not m:
            continue

        val = m.group(1).strip()

        # 如果抓到的是整个区块，再从区块里提名称
        if "名称" in val and ("统一社会信用代码" in val or "纳税人识别号" in val):
            m2 = re.search(r"名称\s*[:：]?\s*(.+?)(?:统一社会信用代码|纳税人识别号|$)", val, re.S)
            if m2:
                val = _valid_name(m2.group(1))
                if val:
                    return val

        # 普通直接抓到名称
        if "统一社会信用代码" in val or "纳税人识别号" in val:
            val = re.split(r"统一社会信用代码|纳税人识别号", val)[0].strip()

        val = _valid_name(val)
        if val:
            return val

    return ""


def _extract_seller_name(text: str) -> str:
    """
    兼容这类成品油电子发票：
    销 售 方 信 息
    名称：中国石油天然气股份有限公司四川销售分公司
    """
    direct_patterns = [
        r"(?:^|\n)\s*(?:买|购)[ \t]+名称\s*[:：]\s*[^\n]+?[ \t]+(?:售|销)[ \t]+名称\s*[:：]\s*([^\n]+)",
        r"销\s+名称\s*[:：]\s*([^\n]+)",
        r"销\s*名称\s*[:：]\s*([^\n]+)",
        r"销售方(?:信息)?名称\s*[:：]?\s*([^\n]+)",
        r"销\s*售\s*方\s*信\s*息名称\s*[:：]\s*([^\n]+)",
    ]
    for p in direct_patterns:
        val = _search(p, text, flags=re.S)
        val = re.split(r"\s+(?:买|售|方|信|统一社会信用代码|纳税人识别号)\b", val)[0]
        val = _valid_name(val)
        if val:
            return val

    patterns = [
        r"销售方(?:信息)?名称\s*[:：]?\s*([^\n]+)",
        r"销\s*售\s*方\s*信\s*息[\s\S]{0,80}?名称\s*[:：]?\s*([^\n]+)",
        r"销\s*售\s*方\s*信\s*息([\s\S]{0,250})",
    ]

    for p in patterns:
        m = re.search(p, text, re.S)
        if not m:
            continue

        val = m.group(1).strip()

        # 如果抓到的是整块，再从里面取“名称”
        if "名称" in val and ("统一社会信用代码" in val or "纳税人识别号" in val):
            m2 = re.search(
                r"名称\s*[:：]?\s*(.+?)(?:统一社会信用代码|纳税人识别号|收款人|复核人|开票人|$)",
                val,
                re.S
            )
            if m2:
                val = _valid_name(m2.group(1))
                if val:
                    return val

        if "统一社会信用代码" in val or "纳税人识别号" in val:
            val = re.split(r"统一社会信用代码|纳税人识别号", val)[0].strip()

        val = _valid_name(val)
        if val:
            return val

    return ""


def _extract_side_by_side_tax_nos(text: str) -> tuple:
    """提取 PDF 文本层按左右栏交错排列在同一行的购销方税号。"""
    label = r"统一社会信用代码(?:\s*/\s*纳税人识别号)?|纳税人识别号"
    match = re.search(
        rf"^\s*信\s+(?:{label})\s*[:：]?\s*([0-9A-Z]{{15,20}})"
        rf"\s+信\s+(?:{label})\s*[:：]?\s*([0-9A-Z]{{15,20}})\s*$",
        text,
        re.M | re.I,
    )
    return match.groups() if match else ("", "")


def _extract_buyer_tax_no(text: str) -> str:
    buyer_tax_no, _ = _extract_side_by_side_tax_nos(text)
    if buyer_tax_no:
        return buyer_tax_no.upper()

    buyer_name = _extract_buyer_name(text)
    if buyer_name:
        normalized = _collapse_cjk_name_spacing(text)
        match = re.search(
            re.escape(_collapse_cjk_name_spacing(buyer_name)) + r"\s*([0-9A-Z]{15,20})",
            normalized,
            re.I,
        )
        if match:
            return match.group(1).upper()

    patterns = [
        r"购买方[\s\S]{0,180}?(?:统一社会信用代码(?:\s*/\s*纳税人识别号)?|纳税人识别号)\s*[:：]?\s*([0-9A-Z]{15,20})",
        r"购\s*买\s*方\s*信\s*息[\s\S]{0,180}?(?:统一社会信用代码(?:\s*/\s*纳税人识别号)?|纳税人识别号)\s*[:：]?\s*([0-9A-Z]{15,20})",
        r"购\s+名称[\s\S]{0,180}?(?:统一社会信用代码(?:\s*/\s*纳税人识别号)?|纳税人识别号)\s*[:：]?\s*([0-9A-Z]{15,20})",
    ]
    return _search(patterns, text, flags=re.S | re.I).upper()


def _extract_seller_tax_no(text: str) -> str:
    _, seller_tax_no = _extract_side_by_side_tax_nos(text)
    if seller_tax_no:
        return seller_tax_no.upper()

    seller_name = _extract_seller_name(text)
    if seller_name:
        normalized = _collapse_cjk_name_spacing(text)
        match = re.search(
            re.escape(_collapse_cjk_name_spacing(seller_name)) + r"\s*([0-9A-Z]{15,20})",
            normalized,
            re.I,
        )
        if match:
            return match.group(1).upper()

    patterns = [
        r"销售方[\s\S]{0,220}?(?:统一社会信用代码(?:\s*/\s*纳税人识别号)?|纳税人识别号)\s*[:：]?\s*([0-9A-Z]{15,20})",
        r"销\s*售\s*方\s*信\s*息[\s\S]{0,220}?(?:统一社会信用代码(?:\s*/\s*纳税人识别号)?|纳税人识别号)\s*[:：]?\s*([0-9A-Z]{15,20})",
        r"销\s+名称[\s\S]{0,220}?(?:统一社会信用代码(?:\s*/\s*纳税人识别号)?|纳税人识别号)\s*[:：]?\s*([0-9A-Z]{15,20})",
    ]
    return _search(patterns, text, flags=re.S | re.I).upper()


def _extract_project_name(text: str) -> str:
    """
    这张发票的项目明细是：
    *汽油*95号 车用汽油(Ⅵ 95#汽油B)
    """
    # 1) 按明细表的实际行提取。部分 PDF 会把名称换成两行，并把后续列粘在第二行。
    lines = [_squash_spaces(line) for line in text.splitlines() if _squash_spaces(line)]
    for index, line in enumerate(lines):
        if not line.startswith("*"):
            continue

        value = _project_name_part(line)
        if index + 1 < len(lines):
            continuation = _project_name_part(lines[index + 1])
            if (
                re.fullmatch(r"[\u3400-\u4dbf\u4e00-\u9fff]{1,12}", continuation or "")
                and continuation not in {"合计", "备注", "价税合计"}
            ):
                value += continuation
        if value:
            return _clean_project_value(value, text)

    # 2) 先抓“项目名称”后面到规格型号/单位/数量前的内容
    m = re.search(r"\n(\*[^¥\n]{2,120}?)(?:\s+\d+(?:\.\d+)?\s+\d+(?:\.\d+)?|\s+升|\s+¥|\s+金额|\s+税率)", text, re.S)
    if m:
        return _clean_project_value(m.group(1), text)

    # 3) 针对这类成品油票，直接抓 * 开头的商品名
    m = re.search(r"(\*[^¥\n]{2,120}?)(?:\s+升|\s+\d+\.\d+|\s+金额|\s+税率)", text, re.S)
    if m:
        return _clean_project_value(m.group(1), text)

    # 4) 再兜底：项目名称下一行整行拿走
    m = re.search(r"项目名称[^\n]*\n([^\n]+)", text, re.S)
    if m:
        return _clean_project_value(m.group(1), text)

    return ""

def _extract_invoice_info_once(path, current_user_name="", invoice_type_hint="", allow_image_extra=True):
    """
    返回字段：
    receipt_date, submitter_name, pdf_filename, invoice_date, invoice_no,
    buyer_name, seller_name, amount, project_name, invoice_type_id, file_path
    """
    text = _extract_text(path)
    invoice_type_hint = invoice_type_hint or _detect_paper_invoice_type_from_filename(path)

    info = {
        "receipt_date": datetime.now().strftime("%Y-%m-%d"),
        "submitter_name": current_user_name or "",
        "pdf_filename": os.path.basename(path),
        "invoice_date": "",
        "invoice_no": "",
        "buyer_name": "",
        "buyer_tax_no": "",
        "seller_name": "",
        "seller_tax_no": "",
        "amount": "",
        "project_name": "",
        "invoice_type_id": "",
    }

    # 没抽到文本就直接返回默认值
    if not text:
        if allow_image_extra:
            image_extra = _extract_digits_from_image_filename(path, invoice_type_hint)
            for key, value in image_extra.items():
                if value:
                    info[key] = value
        return info

    # 调试时可以临时打开，看看 PDF 实际抽出来长什么样
    # print("===== PDF TEXT START =====")
    # print(text)
    # print("===== PDF TEXT END =====")

    info["invoice_date"] = _extract_invoice_date(text)
    info["invoice_no"] = _extract_invoice_no(text)
    info["amount"] = _extract_amount(text)
    info["buyer_name"] = _extract_buyer_name(text)
    info["buyer_tax_no"] = _extract_buyer_tax_no(text)
    info["seller_name"] = _extract_seller_name(text)
    info["seller_tax_no"] = _extract_seller_tax_no(text)
    info["project_name"] = _extract_project_name(text)

    if invoice_type_hint:
        paper_info = _extract_paper_invoice_info(text, invoice_type_hint)
        for key, value in paper_info.items():
            if value:
                info[key] = value
        if allow_image_extra and not _is_good_invoice_info(info, invoice_type_hint):
            image_extra = _extract_digits_from_image_filename(path, invoice_type_hint)
            for key, value in image_extra.items():
                if value:
                    info[key] = value

    return info


def extract_invoice_info(path, current_user_name="", invoice_type_hint=""):
    invoice_type_hint = invoice_type_hint or _detect_paper_invoice_type_from_filename(path)
    if not _is_image_file(path):
        return _extract_invoice_info_once(path, current_user_name, invoice_type_hint)

    best_info = _extract_invoice_info_once(path, current_user_name, invoice_type_hint, allow_image_extra=False)
    best_info["pdf_filename"] = os.path.basename(path)
    merged_info = dict(best_info)
    best_score = _invoice_info_score(best_info, invoice_type_hint)
    if _is_good_invoice_info(best_info, invoice_type_hint):
        return best_info

    # 快速模式只执行一次整图 OCR。旧的三方向旋转和字段增强识别最差会额外
    # 调用 PaddleOCR 六次以上，仅在明确要求更高容错时通过环境变量开启。
    if not PADDLE_OCR_ENABLE_EXTRA_PASS:
        return best_info

    orientation_paths = _make_orientation_paths(path)
    try:
        for oriented_path in orientation_paths:
            info = _extract_invoice_info_once(oriented_path, current_user_name, invoice_type_hint, allow_image_extra=False)
            # 对外仍展示原始上传文件名，不展示临时旋转图文件名。
            info["pdf_filename"] = os.path.basename(path)
            score = _invoice_info_score(info, invoice_type_hint)
            if score > best_score:
                best_score = score
                best_info = info
            for key, value in info.items():
                if key == "pdf_filename":
                    continue
                if value and not merged_info.get(key):
                    merged_info[key] = value
            merged_info["pdf_filename"] = os.path.basename(path)
            if _is_good_invoice_info(merged_info, invoice_type_hint):
                return merged_info
    finally:
        _cleanup_temp_paths(orientation_paths, path)

    if not _is_good_invoice_info(merged_info, invoice_type_hint):
        image_extra = _extract_digits_from_image_filename(path, invoice_type_hint)
        for key, value in image_extra.items():
            if value and not merged_info.get(key):
                merged_info[key] = value
        merged_info["pdf_filename"] = os.path.basename(path)

    if _invoice_info_score(merged_info, invoice_type_hint) >= best_score:
        return merged_info
    return best_info
