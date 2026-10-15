"""Extract uploaded PDF/DOCX bytes locally, preserve originals, and import pending sources."""

import base64
import binascii
import hashlib
import io
import uuid
import zipfile

from .importer import import_documents
from .store import bindings_for, can_create_somewhere, connect, user_record

MAX_FILE_BYTES = 2 * 1024 * 1024


def extract_file(filename, raw):
    suffix = filename.lower().rsplit(".", 1)[-1]
    if suffix == "pdf":
        if not raw.startswith(b"%PDF-"):
            raise ValueError("Nội dung không phải PDF hợp lệ.")
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(raw), strict=False)
            if reader.is_encrypted:
                raise ValueError("PDF có mật khẩu. Hãy dùng bản đã mở khóa.")
            if not 1 <= len(reader.pages) <= 50:
                raise ValueError("PDF cần có từ 1 đến 50 trang.")
            pages = []
            for index, page in enumerate(reader.pages, 1):
                text = (page.extract_text() or "").strip()
                if not text and page.get("/Resources", {}).get("/XObject"):
                    raise ValueError(
                        "PDF có trang ảnh/scan chưa đọc được. Cần OCR hoặc bản PDF có lớp văn bản."
                    )
                pages.append({"page": index, "text": text})
            text = "\n\n".join(p["text"] for p in pages if p["text"])
            extraction = {"extractor": "pypdf-text", "page_count": len(pages), "pages": pages}
            media = "application/pdf"
        except ImportError:
            raise ValueError("Runtime cần thư viện pypdf để nhập PDF.") from None
        except ValueError:
            raise
        except Exception:
            raise ValueError("Không thể đọc PDF. Tệp có thể bị lỗi hoặc cần OCR.") from None
    elif suffix == "docx":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = archive.infolist()
                if len(members) > 1000 or sum(item.file_size for item in members) > 8 * 1024 * 1024:
                    raise ValueError("Word vượt giới hạn nội dung giải nén.")
                if "word/document.xml" not in archive.namelist():
                    raise ValueError("Tệp không phải Word .docx hợp lệ.")
                for member in members:
                    if member.filename.endswith(".xml"):
                        xml = archive.read(member)
                        if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                            raise ValueError("Word chứa cấu trúc XML không được hỗ trợ.")
            from docx import Document
            from docx.text.paragraph import Paragraph

            document = Document(io.BytesIO(raw))
            blocks = []
            for block in document.iter_inner_content():
                if isinstance(block, Paragraph):
                    blocks.append(block.text)
                else:
                    blocks.extend(" | ".join(cell.text for cell in row.cells) for row in block.rows)
            text = "\n".join(blocks).strip()
            extraction = {
                "extractor": "python-docx-body",
                "page_count": None,
                "note": "Trích thân văn bản và bảng; không đọc chữ trong ảnh, comments, header/footer hoặc thay đổi được theo dõi.",
            }
            media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        except ImportError:
            raise ValueError("Runtime cần thư viện python-docx để nhập Word.") from None
        except ValueError:
            raise
        except Exception:
            raise ValueError(
                "Không thể đọc Word. Chỉ hỗ trợ .docx, không hỗ trợ .doc cũ hoặc tệp lỗi."
            ) from None
    else:
        raise ValueError("Chỉ nhận PDF và Word .docx; hãy chuyển .doc cũ sang .docx.")
    if len(text.strip()) < 10:
        raise ValueError("Không trích được đủ văn bản. Tệp ảnh/scan cần OCR.")
    if len(text) > 58000:
        raise ValueError(
            "Văn bản vượt giới hạn 58.000 ký tự; hãy chia tài liệu thành phần nhỏ hơn."
        )
    # Preserve real text, avoiding executable content or guessed metadata.
    return text, media, extraction


def import_file(db_path, payload):
    metadata = payload.get("metadata")
    filename = payload.get("file_name")
    encoded = payload.get("file_base64")
    if (
        not isinstance(metadata, dict)
        or not isinstance(filename, str)
        or not 1 <= len(filename) <= 200
        or "/" in filename
        or "\\" in filename
        or not isinstance(encoded, str)
    ):
        raise ValueError("Cần file PDF/Word và metadata hợp lệ.")
    if len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
        raise ValueError("Tệp vượt giới hạn 2 MB.")
    # Check authorization before parsing any uploaded file.
    with connect(db_path) as conn:
        user = user_record(conn, payload.get("user_id"))
        if not user or not can_create_somewhere(user, bindings_for(conn)):
            raise PermissionError("Chưa được cấp quyền nhập tài liệu.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Dữ liệu file không hợp lệ.") from None
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise ValueError("Tệp cần từ 1 byte đến 2 MB.")
    text, media, extraction = extract_file(filename, raw)
    sid = "FILE-" + uuid.uuid4().hex[:12]
    section = "Văn bản trích xuất"
    source = {
        key: metadata.get(key)
        for key in (
            "title",
            "version",
            "policy_key",
            "effective_from",
            "effective_to",
            "applies_company",
            "applies_department",
            "applies_role",
            "scope_level",
            "allow",
            "deny",
            "facts",
        )
    }
    source.update(
        id=sid, section=section, text=f"# {metadata.get('title','')}\n\n## {section}\n{text}"
    )
    upload = {
        "filename": filename,
        "media_type": media,
        "file_checksum": hashlib.sha256(raw).hexdigest(),
        "original": raw,
        "extraction": extraction,
    }
    result = import_documents(
        db_path,
        {
            "user_id": payload.get("user_id"),
            "bundle": {"format": "mission2-documents-v1", "documents": [source]},
        },
        upload=upload,
    )
    result.update(
        filename=filename,
        characters=len(text),
        pages=extraction.get("page_count"),
        extractor=extraction["extractor"],
    )
    return result


def original_file(db_path, user_id, source_id, review=False):
    from .store import can_read, document_records
    from .control import doc_permissions

    with connect(db_path) as conn:
        user = user_record(conn, user_id)
        doc = next((doc for doc in document_records(conn) if doc["id"] == source_id), None)
        if (
            not user
            or not doc
            or not (
                bool(doc_permissions(user, bindings_for(conn), doc))
                if review
                else can_read(user, doc)
            )
        ):
            return None
        row = conn.execute(
            "SELECT original,media_type FROM document_uploads WHERE document_id=?", (source_id,)
        ).fetchone()
        return dict(row) if row else None
