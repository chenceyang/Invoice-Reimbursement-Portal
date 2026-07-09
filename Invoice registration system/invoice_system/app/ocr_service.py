import os
import re
from datetime import datetime
from PyPDF2 import PdfReader
import pdfplumber


def _normalize_date(s: str) -> str:
    """把 2026年7月7日 / 2026-7-7 / 2026/7/7 统一成 YYYY-MM-DD"""
    if not s:
        return ""
    s = str(s).strip()
    s = s.replace("年", "-").replace("月", "-").replace("日", "")
    s = s.replace("/", "-").replace(".", "-").replace(" ", "")
    m = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
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
        ("开 票 日 期", "开票日期"),
        ("价 税 合 计", "价税合计"),
        ("购 买 方 信 息", "购买方信息"),
        ("销 售 方 信 息", "销售方信息"),
        ("项 目 名 称", "项目名称"),
        ("小 写", "小写"),
        ("名 称", "名称"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def _extract_text(path: str) -> str:
    """
    目前先做 PDF 文本抽取。
    如果后续你要接 OCR（图片 / 扫描版 PDF），再把图片 OCR 接进来。
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


def _valid_name(value: str) -> str:
    value = _squash_spaces(value)
    value = value.strip(" ：:")
    if not value or value in {"名称", "名称："}:
        return ""
    return value


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


def _extract_invoice_date(text: str) -> str:
    # 先抓“开票日期/票据日期”
    val = _search([
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


def _extract_project_name(text: str) -> str:
    """
    这张发票的项目明细是：
    *汽油*95号 车用汽油(Ⅵ 95#汽油B)
    """
    # 1) 先抓“项目名称”后面到规格型号/单位/数量前的内容
    m = re.search(r"\n(\*[^¥\n]{2,120}?)(?:\s+\d+(?:\.\d+)?\s+\d+(?:\.\d+)?|\s+升|\s+¥|\s+金额|\s+税率)", text, re.S)
    if m:
        return _clean_project_value(m.group(1), text)

    # 2) 针对这类成品油票，直接抓 * 开头的商品名
    m = re.search(r"(\*[^¥\n]{2,120}?)(?:\s+升|\s+\d+\.\d+|\s+金额|\s+税率)", text, re.S)
    if m:
        return _clean_project_value(m.group(1), text)

    # 3) 再兜底：项目名称下一行整行拿走
    m = re.search(r"项目名称[^\n]*\n([^\n]+)", text, re.S)
    if m:
        return _clean_project_value(m.group(1), text)

    return ""

def extract_invoice_info(path, current_user_name=""):
    """
    返回字段：
    receipt_date, submitter_name, pdf_filename, invoice_date, invoice_no,
    buyer_name, seller_name, amount, project_name, invoice_type_id, file_path
    """
    text = _extract_text(path)

    info = {
        "receipt_date": datetime.now().strftime("%Y-%m-%d"),
        "submitter_name": current_user_name or "",
        "pdf_filename": os.path.basename(path),
        "invoice_date": "",
        "invoice_no": "",
        "buyer_name": "",
        "seller_name": "",
        "amount": "",
        "project_name": "",
        "invoice_type_id": "",
    }

    # 没抽到文本就直接返回默认值
    if not text:
        return info

    # 调试时可以临时打开，看看 PDF 实际抽出来长什么样
    # print("===== PDF TEXT START =====")
    # print(text)
    # print("===== PDF TEXT END =====")

    info["invoice_date"] = _extract_invoice_date(text)
    info["invoice_no"] = _extract_invoice_no(text)
    info["amount"] = _extract_amount(text)
    info["buyer_name"] = _extract_buyer_name(text)
    info["seller_name"] = _extract_seller_name(text)
    info["project_name"] = _extract_project_name(text)

    return info
