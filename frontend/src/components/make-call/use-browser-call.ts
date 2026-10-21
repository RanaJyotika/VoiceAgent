"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Browser test call: mic -> WebSocket -> Python server -> agent -> WebSocket -> speaker. Free: no phone line.
// The same protocol as the original dashboard (backend/app/static/app.js): 16 kHz PCM up, 24 kHz PCM down,
// JSON text events alongside.

const OUT_RATE = 24000;

export type Line = { id: number; role: "agent" | "customer" | "meta" | "error"; text: string };

/** reason: what this call is about, typed in the dialog; empty = the contact's own call type. */
type Options = { contactId: number; voice: string; model: string; language: string; build: string; reason?: string };

/** The Python server's address for the WebSocket. `npm run dev` (port 3000) cannot forward WebSockets reliably,
 *  so in development the page talks to the Python server on port 8000 directly. */
function socketUrl(path: string) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const host = location.port === "3000" ? `${location.hostname}:8000` : location.host;
  return `${proto}://${host}${path}`;
}

export function useBrowserCall() {
  const [running, setRunning] = useState(false);
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState("");
  const [lines, setLines] = useState<Line[]>([]);
  const [level, setLevel] = useState(0); // mic level 0..100
  const [muted, setMuted] = useState(false);

  const r = useRef({
    ws: null as WebSocket | null,
    ctx: null as AudioContext | null,
    mic: null as MediaStream | null,
    node: null as AudioWorkletNode | null,
    playhead: 0,
    scheduled: [] as AudioBufferSourceNode[],
    agentLine: null as number | null, // id of the line the agent is still speaking into
    customerLine: null as number | null,
    nextId: 1,
    muted: false,
  });

  const addLine = useCallback((role: Line["role"], text: string) => {
    const id = r.current.nextId++;
    setLines((ls) => [...ls, { id, role, text }]);
    return id;
  }, []);
  const appendTo = useCallback((id: number, text: string) => setLines((ls) => ls.map((l) => (l.id === id ? { ...l, text: l.text + text } : l))), []);

  const stopPlayback = useCallback(() => {
    for (const s of r.current.scheduled) {
      try {
        s.stop();
      } catch {
        /* already stopped */
      }
    }
    r.current.scheduled = [];
    r.current.playhead = 0;
  }, []);

  const playChunk = useCallback((data: ArrayBuffer) => {
    const ctx = r.current.ctx;
    if (!ctx) return;
    const int16 = new Int16Array(data);
    const f32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) f32[i] = int16[i] / 0x8000;
    const buf = ctx.createBuffer(1, f32.length, OUT_RATE);
    buf.getChannelData(0).set(f32);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    const now = ctx.currentTime;
    if (r.current.playhead < now) r.current.playhead = now + 0.02;
    src.start(r.current.playhead);
    r.current.playhead += buf.duration;
    r.current.scheduled.push(src);
    src.onended = () => {
      r.current.scheduled = r.current.scheduled.filter((s) => s !== src);
    };
  }, []);

  const cleanup = useCallback(() => {
    const c = r.current;
    stopPlayback();
    try {
      c.node?.disconnect();
    } catch {
      /* gone */
    }
    c.node = null;
    c.mic?.getTracks().forEach((t) => t.stop());
    c.mic = null;
    c.ctx?.close().catch(() => {});
    c.ctx = null;
    if (c.ws) {
      c.ws.onclose = c.ws.onerror = c.ws.onmessage = null;
      try {
        c.ws.close();
      } catch {
        /* closed */
      }
    }
    c.ws = null;
    setRunning(false);
    setConnected(false);
    setLevel(0);
  }, [stopPlayback]);

  const start = useCallback(
    async ({ contactId, voice, model, language, build, reason = "" }: Options) => {
      const c = r.current;
      setLines([]);
      c.agentLine = c.customerLine = null;
      setRunning(true);
      setStatus("Starting the microphone…");

      const ctx = new AudioContext();
      c.ctx = ctx;
      try {
        await ctx.audioWorklet.addModule("/static/pcm-processor.js");
        c.mic = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
      } catch {
        setStatus("Microphone blocked. Use Chrome or Edge and click Allow when it asks for the microphone.");
        addLine("error", "Microphone permission was denied, so the test was not started.");
        cleanup();
        return;
      }
      const node = new AudioWorkletNode(ctx, "pcm-processor");
      c.node = node;
      ctx.createMediaStreamSource(c.mic).connect(node);
      // Keep the worklet in the audio graph (silent) so the browser always runs it.
      const silent = ctx.createGain();
      silent.gain.value = 0;
      node.connect(silent).connect(ctx.destination);

      const qs = new URLSearchParams({ voice, model, language, build, reason });
      const ws = new WebSocket(socketUrl(`/ws/call/${contactId}?${qs}`));
      ws.binaryType = "arraybuffer";
      c.ws = ws;

      ws.onopen = () => {
        setConnected(true);
        setStatus("Connected. The agent will greet you in 1 to 3 seconds. Wait for it, then reply.");
        node.port.onmessage = (e: MessageEvent<ArrayBuffer>) => {
          const s = new Int16Array(e.data);
          let sum = 0;
          for (let i = 0; i < s.length; i++) sum += s[i] * s[i];
          setLevel(Math.min(100, Math.round((Math.sqrt(sum / s.length) / 32768) * 600)));
          // Muted still sends audio, but silent, so the agent's turn-taking keeps working.
          if (ws.readyState === 1) ws.send(c.muted ? new ArrayBuffer(e.data.byteLength) : e.data);
        };
      };
      ws.onmessage = (e) => {
        if (e.data instanceof ArrayBuffer) return playChunk(e.data);
        const m = JSON.parse(e.data);
        switch (m.type) {
          case "status":
            setStatus(m.text);
            break;
          case "interrupted":
            stopPlayback();
            addLine("meta", "customer interrupted");
            c.agentLine = null;
            break;
          case "transcript":
            if (m.role === "agent") {
              if (c.agentLine == null) {
                c.agentLine = addLine("agent", "");
                setStatus("Agent is speaking…");
              }
              appendTo(c.agentLine, m.text);
            } else {
              if (c.customerLine == null) c.customerLine = addLine("customer", "");
              appendTo(c.customerLine, m.text);
            }
            break;
          case "turn_complete":
            c.agentLine = c.customerLine = null;
            setStatus("Agent finished speaking. Your turn: speak, or type a reply below.");
            break;
          case "outcome":
            addLine("meta", `outcome: ${m.outcome}${m.details && Object.keys(m.details).length ? ` ${JSON.stringify(m.details)}` : ""}`);
            break;
          case "error":
            addLine("error", m.text);
            break;
          case "ended":
            setStatus(`Call ended (${m.duration_sec}s, outcome: ${m.outcome || "none"}).`);
            cleanup();
            break;
        }
      };
      ws.onclose = () => cleanup();
      ws.onerror = () => {
        addLine("error", "Lost the connection to the server.");
        cleanup();
      };
    },
    [addLine, appendTo, cleanup, playChunk, stopPlayback],
  );

  const hangup = useCallback(() => {
    const ws = r.current.ws;
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "hangup" }));
    setStatus("Test ended.");
    cleanup();
  }, [cleanup]);

  const sendText = useCallback(
    (text: string) => {
      const ws = r.current.ws;
      const t = text.trim();
      if (!t || !ws || ws.readyState !== 1) return false;
      stopPlayback();
      ws.send(JSON.stringify({ type: "text", text: t }));
      addLine("customer", `(typed) ${t}`);
      r.current.customerLine = null;
      return true;
    },
    [addLine, stopPlayback],
  );

  const toggleMute = useCallback(() => {
    r.current.muted = !r.current.muted;
    setMuted(r.current.muted);
  }, []);

  // Leaving the page mid-test ends the call and frees the microphone.
  useEffect(() => {
    const c = r.current; // the same object for the hook's whole life
    return () => {
      if (c.ws?.readyState === 1) c.ws.send(JSON.stringify({ type: "hangup" }));
      c.ws?.close();
      c.mic?.getTracks().forEach((t) => t.stop());
      c.ctx?.close().catch(() => {});
    };
  }, []);

  return { running, connected, status, lines, level, muted, start, hangup, sendText, toggleMute };
}
