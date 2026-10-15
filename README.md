# DEMO RAG

Demo hỏi đáp tri thức nội bộ có phân quyền theo tổ chức, quản lý phiên bản và trích dẫn nguồn.

## Chạy ứng dụng

Cần Python 3.10 trở lên. Trong thư mục repository:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python server.py --port 8765
```

Trên macOS/Linux, kích hoạt môi trường bằng `source .venv/bin/activate`.
Mở http://127.0.0.1:8765/. Server chỉ lắng nghe localhost.

Database SQLite được tự tạo tại `runtime/mission2-v2.sqlite3` từ `data/seed.json` và `data/sources/`.
Dữ liệu seed đều giả lập; đây không phải bản sao database đang chạy trên máy phát triển.
Seed ban đầu có mức hỗ trợ An 80.000 đồng/ca, Bình 40.000 đồng/ca; bản 60.000 đồng đã chỉnh trên máy phát triển không nằm trong repo này.

## Chức năng

- Hỏi đáp theo công ty, phòng ban, vai trò và ngày áp dụng.
- Quyền đọc, tạo bản sửa, duyệt, công bố và quản lý ACL được kiểm riêng bằng database.
- Import PDF có lớp văn bản hoặc Word DOCX; giữ file gốc.
- Semantic parent-child chunking; tìm kiếm dense và BM25 kết hợp RRF.
- Sinh và kiểm chứng đáp án qua Groq/OpenAI hoặc model local.
- Trích dẫn theo câu; mở đoạn hoặc toàn bộ tài liệu vẫn giữ highlight.
- Tài liệu dùng chung A/B qua phạm vi Tập đoàn / nhiều công ty, với quyền được cấp tương ứng.
- Duyệt tài liệu công ty theo chuỗi giám đốc → trưởng phòng → công bố.
- Tạo phiên bản mới, quản lý thay thế/ngoại lệ, hiệu lực và thu hồi.
- Báo lỗi kèm ngữ cảnh xử lý, chỉ người có quyền được xem.

## Thiết lập model

Database mới chạy chế độ trích xuất nếu chưa cấu hình model. Cần cài Ollama để dùng embedding local.
Ví dụ trên Windows PowerShell:

```powershell
./scripts/setup/start_models.ps1
ollama pull qwen3-embedding:0.6b
ollama pull qwen3.5:4b
python scripts/setup/setup_rag.py
python scripts/setup/setup_api.py
```

`setup_rag.py` hiện kiểm cả hai model và chốt digest trước khi lập chỉ mục.
Để dùng Groq/OpenAI, chọn hồ sơ Dũng hoặc Hà → Cấu hình LLM, nhập key và kiểm tra kết nối.
Embedding vẫn local. Key API chỉ giữ trong RAM server; khởi động lại phải nhập lại.

## Hồ sơ mô phỏng

A và B ngang cấp, cùng thuộc tập đoàn. An là nhân viên Vận hành A; Bình là nhân viên Kinh doanh B.
Dũng là chủ tịch tập đoàn; Minh/Linh là giám đốc A/B; các trưởng phòng duyệt trong phạm vi được giao.
Danh sách chọn hồ sơ là tính năng mô phỏng, chưa có đăng nhập/SSO production.

## Cấu trúc source

```text
app/                  Logic quyền, RAG, import, phiên bản, report và giao diện static
data/                 Seed tổ chức/quyền và tài liệu giả lập để khởi tạo
database/schema.sql   SQL tạo cấu trúc SQLite, không có dữ liệu runtime
scripts/setup/        Thiết lập model, migration và chỉ mục
server.py             Điểm chạy HTTP server
run.ps1               Lệnh chạy tiện dụng trên Windows
requirements.txt      Dependency để chạy ứng dụng
```

`app/store.py:SCHEMA` vẫn là định nghĩa SQL mà ứng dụng sử dụng; `database/schema.sql` là bản xuất tham khảo.
Repo chỉ có source và dữ liệu khởi tạo cần thiết, không kèm tests, báo cáo kiểm thử, file đã import, database, model, log hoặc API key.

## Giới hạn

PDF scan cần OCR; bảng, công thức và nhiều cột có thể mất cấu trúc khi trích xuất.
Tìm kiếm Việt–Anh và kiểm chứng LLM vẫn cần đánh giá trên dữ liệu thực tế.
Đây là demo localhost, chưa có SSO, streaming hoặc kiểm tải production.
