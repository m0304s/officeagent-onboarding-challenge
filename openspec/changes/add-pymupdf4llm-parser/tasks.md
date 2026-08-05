## 1. 픽스처로 판정을 먼저 확인한다

design 의 첫 번째 위험이 "헤딩·표 판정이 합성 PDF 에서 안 잡힐 수 있다"이다. 구현보다 이것을 먼저 확인한다 — 안 잡히면 스펙을 고쳐야 하고, 그 판단이 구현 뒤로 밀리면 테스트를 느슨하게 만들고 싶어진다.

- [ ] 1.1 `pyproject.toml` 에 **`pymupdf4llm==1.28.0`** 을 더하고, 기존 `pymupdf>=1.24` 를 **`pymupdf==1.28.0`** 으로 바꾼다(그 버전이 정확히 요구하는 값이다). 주석에는 고정하는 이유 — 추출 결과가 저장물이고 저장물이 서명 축이다 — 를 적는다. 정확 고정은 `chromadb==1.5.9` 와 같은 처리다
- [ ] 1.1a **의존성이 실제로 무엇을 끌고 오는지 확인한다.** `docker compose build` 가 통과하는지, 설치된 목록에 `pymupdf_layout`·`onnxruntime` 이 들어오는지, 그리고 **첫 추출이 네트워크 없이 끝나는지**(레이아웃 모델을 첫 사용 시점에 받으면 평가자 환경의 첫 업로드가 네트워크에 묶인다). 받는 것이 있으면 빌드 단계로 옮긴다
- [ ] 1.2 `tests/pdf_fixtures.py` 에 구조가 있는 PDF 생성기를 더한다 — **조판 크기 세 가지**(본문·중간 제목·큰 제목), 격자선으로 그린 표(셀 안 텍스트 포함), 여러 쪽. 크기 차이는 헤딩 판정이 폰트 크기 분포로 이루어지므로 넉넉히 벌린다. 기존 생성기는 손대지 않는다
- [ ] 1.3 컨테이너에서 그 픽스처를 마크다운으로 변환해 **눈으로 확인한다** — 제목에 `#` 이 붙는지, **두 제목의 `#` 개수가 다르고 큰 쪽이 더 적은지**, 표가 `|` 로 나오는지, 쪽 경계가 유지되는지. `docker compose run --build --rm test python -c ...` 로 출력만 찍어 본다. 판정이 안 잡히면 픽스처 조판을 조정하고(헤딩은 `body_limit` 기준의 폰트 크기 분포로 갈린다), 조정으로도 안 되면 **구현을 시작하기 전에** `/opsx:update` 로 스펙을 고친다

## 2. 추출 방식 선택 축

- [ ] 2.1 `adapters/parsers/selection.py` 에 `PdfExtraction` 열거형(`markdown` · `plain`), `PDF_EXTRACTION_VERSION` 상수, `PdfExtractionChoice`(모드·파서·`signature_material`), `select_pdf_extraction(mode)` 를 둔다. **파서와 서명 재료가 이 객체 하나에서 함께 나온다** — 어긋나려면 `select_pdf_extraction` 을 두 번 불러야 한다 (design 결정 2)
- [ ] 2.2 `config.py` 에 `pdf_extraction` 설정을 더한다. 기본값 `markdown`, 알 수 없는 값은 pydantic 검증이 기동에서 막는다 (`ChunkStrategy` 와 같은 처리)
- [ ] 2.3 `registry.py` 의 `default_parsers(choice)` 가 선택 결과를 받아 PDF 파서 **하나만** 등록하게 한다. **기본 인자를 두지 않는다** — 기본값이 있는 한 "서명은 지정했는데 파서는 기본값"인 하네스가 조용히 통과한다. 중복 등록 금지는 그대로 둔다
- [ ] 2.4 `tests/test_config.py` — 설정 없이 기동하면 `markdown`, 알 수 없는 값은 기동 실패이며 오류 메시지에 받아들여지는 값 목록이 포함되는 것 (스펙 「설정 없이 기동하면 기본 추출 방식이 쓰인다」·「구현되지 않은 추출 방식 값은 기동을 막는다」)

## 3. 구조 보존 파서

- [ ] 3.1 `adapters/parsers/pdf_markdown.py` 에 마크다운 PDF 파서를 만든다. **`pdf.py` 는 열지 않는다.** 0바이트는 빈 `ExtractedDocument`, 라이브러리 import 는 함수 안, 예외는 경계에서 `DocumentParseError` 로 끊는다
- [ ] 3.2 문서를 우리가 열고(`pymupdf.open`) `needs_pass` 를 먼저 판정한 뒤, 열린 문서를 `to_markdown(document, page_chunks=True, page_separators=False, write_images=False)` 에 넘긴다 (design 결정 3). `page_count` 는 문서에서 읽는다 — 변환 결과의 길이로 세지 않는다. **평문 추출로 물러서는 폴백을 두지 않는다**
- [ ] 3.2a 쪽 조각에서 `text` 만 쓴다. `toc_items`·`tables`·`images`·`graphics` 는 담을 자리가 없으므로 이번에는 버린다 — 쓰는 쪽이 정해질 때 `ExtractedDocument` 의 모양과 함께 정한다 (design 「계층형 인덱싱 검토」)
- [ ] 3.3 쪽별 조각을 `TextSegment(text=…, page=n)` 으로 옮긴다. **쪽 번호는 조각의 `metadata["page_number"]` 에서 읽고, 없으면 `DocumentParseError` 로 던진다** — 목록 순번으로 세면 변환기가 쪽을 건너뛸 때 출처가 통째로 밀리는데 오류가 나지 않는다. 개행 정규화는 기존 파서와 같은 헬퍼(`normalization.normalize_newlines`)를 쓴다
- [ ] 3.4 **구조 기호만 남은 쪽을 버린다** (design 결정 4) — `#`·`|`·`-`·`*`·`>`·공백 뿐인 쪽은 텍스트가 없는 쪽이다. 판정에만 쓰고 저장 본문은 변환 결과 그대로 둔다. 이 처리가 없으면 스캔본이 `no_extractable_text` 대신 구분선 청크로 저장된다
- [ ] 3.5 `adapters/parsers/__init__.py` 의 공개 이름에 새 파서를 더한다
- [ ] 3.6 `src/app/core/.ruff.toml` 의 `banned-api` 에 `pymupdf4llm` 을 더한다 — PDF 파싱이 `core/` 로 새는 것을 기존 규칙과 같은 방식으로 막는다

## 4. 파서 계약 테스트 (두 구현 공통)

- [ ] 4.1 `tests/test_parsers.py` 의 PDF 계약 테스트를 **두 파서로 파라미터화**한다. `pymupdf` 가 `1.24` 대에서 `1.28.0` 으로 올라갔으므로 **기존 `plain` 파서 테스트가 그대로 통과하는지도 여기서 드러난다** — 쪽 번호(1-base·쪽 수 초과 없음), 0바이트, 텍스트 없는 PDF, PDF 가 아닌 바이트, 암호 PDF, 내부 예외 메시지가 새지 않는 것 (스펙 「방식에 무관한 계약」의 시나리오 여섯)
- [ ] 4.2 `tests/test_parsers.py` 에 구조 보존 테스트를 더한다 — 제목의 `#`, **두 제목의 `#` 개수가 다르고 큰 쪽이 더 적은 것**(후속 계층형 인덱싱의 재료), 본문 줄에는 헤딩 표기가 없는 것, 표의 `|` 와 같은 행의 셀들이 한 줄에 오는 것, 원문 문장·수치가 변형 없이 남는 것, 두 방식이 같은 PDF 에서 서로 다른 텍스트를 내는 것 (스펙 「기본 추출 방식은 PDF 의 구조를 마크다운으로 보존한다」)
- [ ] 4.3 여러 쪽 픽스처로 쪽 경계 보존을 단언한다 — 각 청크의 페이지 번호가 그 내용이 실제로 있던 쪽과 같고, 서로 다른 쪽의 내용이 한 세그먼트에 섞이지 않는 것
- [ ] 4.4 `docker compose run --build --rm test pytest tests/test_parsers.py -q` 로 확인한다. `--build` 를 빠뜨리면 이전 이미지의 코드를 검사한다 (`CLAUDE.md` 검증 절차)

## 5. 색인 서명과 배선

- [ ] 5.0 `selection.py` 의 `PDF_EXTRACTION_VERSION` 옆에 **핀을 올리면 이 값도 올린다**는 규칙을 한 줄로 적는다. 라이브러리 버전이 바뀌면 추출 결과가 달라질 수 있고, 서명이 그것을 모르면 옛 청크가 새 청크와 한 인덱스에 남는다 (design 결정 3)
- [ ] 5.1 `core/documents.py` 의 `derive_index_signature` 에 재료 **한 칸**(`pdf_extraction_signature`)을 더한다. 값은 `PdfExtractionChoice.signature_material`(`"markdown:v1"` 꼴)이고, 이름·버전을 두 인자로 쪼개지 않는다 — `tokenizer_signature` 선례를 따른다
- [ ] 5.2 `tests/test_documents.py` 의 `BASE_SIGNATURE_MATERIALS` 에 새 재료를 더한다. `parameters == set(BASE_SIGNATURE_MATERIALS)` 단언이 깨지는 것이 정상이며, **이 갱신이 "재료를 늘렸다"는 선언이다.** 방식만 다른 두 값이 다른 서명을 내는 것도 여기서 단언한다
- [ ] 5.3 `main.py` 가 `select_pdf_extraction(settings.pdf_extraction)` 을 **한 번** 부르고, 그 결과에서 서명 재료와 파서를 함께 꺼낸다. 서명은 계속 한 곳에서만 유도한다
- [ ] 5.4 `tests/ingestion_harness.py`·`tests/retrieval_harness.py` 가 `PdfExtractionChoice` 를 받아 **서명과 파서를 같은 객체에서** 꺼내게 한다. 하네스에서 한쪽만 지정하는 길을 남기지 않는다 (design 결정 2의 실패가 여기서 난다)
- [ ] 5.5 `tests/test_ingestion.py`(또는 서명 테스트가 있는 파일) — 추출 방식만 다른 두 구성에서 서명이 다르고 `revision` 은 같은 것, **텍스트 문서에도 그 차이가 반영되는 것**(과잉 무효화가 의도된 동작이라는 단언), 다른 축의 기존 시나리오가 그대로인 것

## 6. 재색인 경로

- [ ] 6.1 `tests/test_ingestion_pipeline.py` — 추출 방식을 바꾸고 같은 저장소로 다시 기동하면 문서가 `stale` 이 되고 양쪽 색인의 청크가 0인 것 (스펙 「추출 방식을 바꾸고 기동하면 PDF 문서가 stale 이 된다」)
- [ ] 6.2 같은 바이트 재업로드가 `unchanged` 가 아니라 재색인이 되고, 저장된 청크 본문이 새 방식의 결과인 것을 단언한다. **새 무효화 코드를 쓰지 않고** 기존 경로가 그대로 도는지가 확인 대상이다
- [ ] 6.2a 캐시 쪽 단언을 더한다 — 추출 방식을 바꾸면 같은 질문이 캐시 히트가 아니라 미스가 되는 것(키·스코프가 서명을 포함한다), 그리고 `markdown → plain → markdown` 으로 **서명이 되돌아왔을 때** 옛 항목이 되살아나지 않는 것(`_verify` 의 현재성 재검증). 새로 만드는 동작이 아니라 **이번 변경이 기존 보호막 안에 있음을 고정하는** 회귀 테스트다
- [ ] 6.3 `docker compose run --build --rm test` 로 전체 스위트를 돌린다. 핵심 3경로(ingestion·retrieval·캐시 무효화)가 기본 구성에서 새 추출기를 타므로 회귀가 여기 드러난다

## 7. 문서 일치

문서-코드 불일치는 감점, 없는 기능을 있다고 적으면 불합격이다. 구현과 같은 커밋에서 고친다.

- [ ] 7.1 `ARCHITECTURE.md` 「문서 파서」 표와 「PDF는 PyMuPDF — 교체 지점은 파일 하나」 절을 고친다. **"교체 지점은 파일 하나"가 더 이상 참이 아니다** — 구현이 둘이고 선택이 설정이라는 사실, 결과의 모양으로 이름 붙인 이유, 서명에 추출 구성을 넣은 이유(포맷별로 쪼개지 않은 근거 포함)를 적는다
- [ ] 7.2 `README.md` 기술 선택 표와 설정 목록에 `APP_PDF_EXTRACTION` 을 더한다. 기본값과 되돌리기 방법, 되돌린 뒤 재업로드가 필요하다는 사실을 함께 적는다
- [ ] 7.3 `tests/README.md` 에 새 픽스처(구조가 있는 PDF)와 계약 테스트를 두 구현으로 파라미터화한 이유를 적는다 — 지켜야 하는 것이 구현 공유가 아니라 관측되는 계약의 동일성이라는 것 (design 결정 6)
- [ ] 7.4 `python3 scripts/check_comments.py` 로 주석 규칙 위반 0건을 확인하고, `docker compose run --build --rm test ruff check .` 로 린트를 확인한다

## 8. 실물 확인

- [ ] 8.1 `docker compose up -d --build --wait` 로 띄운다. `--build` 없이는 이전 세대 이미지가 돈다
- [ ] 8.2 기존 `./data` 의 문서를 지우고 `sample-docs/` 를 다시 올린 뒤, 표·제목이 있는 PDF 하나를 올려 저장된 청크에 마크다운 표기가 실제로 남았는지 확인한다
- [ ] 8.3 `/qa` 로 표 안의 값을 묻는 질문을 던져 답변과 출처(페이지 번호)를 확인한다. 검색 0건이면 `./data` 에 이전 세대가 남아 있는지부터 의심한다 (`CLAUDE.md` 검증 절차)
