import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// API 서버는 CORS 를 열지 않는다. 브라우저가 5173 한 출처만 보게 해서 프리플라이트 자체를
// 없앤다 — 데모 하나 때문에 서버의 미들웨어 체인을 넓히지 않으려는 선택이다 (design D1).
const target = process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000";

// 터널·리버스 프록시 뒤에서는 Host 헤더가 5173 이 아니라 공개 도메인이라, Vite 가 기본으로
// 그 요청을 차단한다. 노출할 도메인을 쉼표로 넘겨 허용한다 (예: VITE_ALLOWED_HOSTS=app.example.com).
const allowedHosts = (process.env.VITE_ALLOWED_HOSTS ?? "")
  .split(",")
  .map((host) => host.trim())
  .filter(Boolean);

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    allowedHosts,
    proxy: {
      "/api": {
        target,
        changeOrigin: true,
        // 라우터가 접두사 없이 마운트돼 있다 (`/qa`, `/documents`).
        rewrite: (path) => path.replace(/^\/api/, ""),
        // 프록시가 응답을 모으면 조각의 도착 시각이 뭉개져 스트리밍이 사라진다.
        selfHandleResponse: false,
      },
    },
  },
});
