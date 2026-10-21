import type { AppConfig } from "./api";

export const LANG_LABELS: Record<string, string> = { hinglish: "Hinglish", hindi: "Hindi", english: "English" };

/** The voices an agent speaks in: Sarvam its own (Priya, Varun…), Gemini and ChatGPT the rest. */
export function voicesFor(cfg: AppConfig, model: string) {
  const sarvam = model.startsWith("sarvam");
  return Object.entries(cfg.voices).filter(([name]) => name in cfg.sarvam_voices === sarvam);
}

/** Keep the chosen voice if the agent has it, else one of the same gender, else the first. */
export function fitVoice(cfg: AppConfig, model: string, want: string) {
  const list = voicesFor(cfg, model);
  if (list.some(([n]) => n === want)) return want;
  const gender = cfg.voices[want];
  return (list.find(([, g]) => g === gender) ?? list[0])?.[0] ?? "";
}

export const remembered = (key: string) => {
  try {
    return localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
};

export const remember = (key: string, value: string) => {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode */
  }
};
