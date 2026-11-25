import { createRoot } from "react-dom/client";
import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
// MapLibre 6 ships its web worker as a separate ES module that bundlers don't emit on their own.
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./jv-tokens.css";
import "./styles.css";
import App from "./App";

maplibregl.setWorkerUrl(workerUrl);

createRoot(document.getElementById("root")!).render(<App />);
