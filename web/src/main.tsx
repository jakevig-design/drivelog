import { createRoot } from "react-dom/client";
import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
// MapLibre 6 ships its web worker as a separate ES module that bundlers don't emit on their own.
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import "./styles.css";
import App from "./App";

maplibregl.setWorkerUrl(workerUrl);

createRoot(document.getElementById("root")!).render(<App />);
