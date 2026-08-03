// SSE 프레이밍의 소비 쪽. `EventSource` 를 못 쓰는 이유는 그것이 GET 만 보내는데
// `/qa` 는 POST + JSON 본문이기 때문이다 (`demo-ui/DESIGN.md`).

export interface SseFrame {
  event: string;
  data: string;
}

/** 프레임 경계에 걸친 조각을 버퍼에 남기는 파서. 호출자는 `push` 를 반복하고 끝에 `flush`. */
export function createSseParser() {
  let buffer = "";

  function drain(final: boolean): SseFrame[] {
    const frames: SseFrame[] = [];
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = parseFrame(buffer.slice(0, boundary));
      if (frame) frames.push(frame);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
    // 서버가 마지막 프레임을 빈 줄 없이 끝냈을 때만 남은 버퍼를 쓴다.
    if (final && buffer.trim() !== "") {
      const frame = parseFrame(buffer);
      if (frame) frames.push(frame);
      buffer = "";
    }
    return frames;
  }

  return {
    push(chunk: string): SseFrame[] {
      // 줄 끝을 먼저 통일한다. 프록시가 CRLF 로 바꿔 보내면 경계 탐색이 통째로 빗나간다.
      buffer += chunk.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      return drain(false);
    },
    flush(): SseFrame[] {
      return drain(true);
    },
  };
}

function parseFrame(raw: string): SseFrame | null {
  let event = "message";
  const data: string[] = [];

  for (const line of raw.split("\n")) {
    // 하트비트가 이 갈래로 빠진다. 이벤트가 아니라 주석이라 계약에 나타나지 않는다.
    if (line.startsWith(":") || line === "") continue;

    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);

    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }

  // 데이터가 없는 프레임은 흘려보내지 않는다 — SSE 규약이 그렇고, 주석만 든 프레임이
  // 이벤트로 승격되면 소비자가 빈 페이로드를 파싱하려 든다.
  return data.length === 0 ? null : { event, data: data.join("\n") };
}

/**
 * 응답 본문을 프레임 스트림으로. 한글이 UTF-8 3바이트라 `stream: true` 가 없으면
 * 청크 경계에서 글자가 깨진다 — 짧은 답변에서는 재현되지 않아 늦게 발견되는 결함이다.
 */
export async function* readSseFrames(body: ReadableStream<Uint8Array>): AsyncGenerator<SseFrame> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  const parser = createSseParser();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      yield* parser.push(decoder.decode(value, { stream: true }));
    }
    yield* parser.push(decoder.decode());
    yield* parser.flush();
  } finally {
    reader.releaseLock();
  }
}
