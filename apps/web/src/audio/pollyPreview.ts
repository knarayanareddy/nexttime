/**
 * Deterministic in-memory WAV preview generator for counterpart turns when running
 * in offline GitHub Pages sandbox mode or local fixture mode without live AWS Polly S3 clips.
 * Audio never autoplays on page open (ui/DESIGN.md §7).
 */

function writeAscii(view: DataView, offset: number, text: string): void {
  for (let i = 0; i < text.length; i += 1) {
    view.setUint8(offset + i, text.charCodeAt(i));
  }
}

export function createPollyFixtureWavDataUri(seedKey: string, durationSec = 1.35): string {
  const sampleRate = 16000;
  const totalSamples = Math.floor(sampleRate * durationSec);
  const dataSize = totalSamples * 2;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);

  writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeAscii(view, 8, "WAVE");
  writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // Mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(view, 36, "data");
  view.setUint32(40, dataSize, true);

  let hash = 2166136261;
  for (let i = 0; i < seedKey.length; i += 1) {
    hash ^= seedKey.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  const baseFreq = 196 + (Math.abs(hash) % 48);
  const notes = [baseFreq, baseFreq * 1.189, baseFreq * 1.335, baseFreq * 1.122];

  for (let i = 0; i < totalSamples; i += 1) {
    const t = i / sampleRate;
    const segmentIndex = Math.min(
      notes.length - 1,
      Math.floor((i / totalSamples) * notes.length)
    );
    const freq = notes[segmentIndex] ?? baseFreq;
    const segProgress = ((i / totalSamples) * notes.length) % 1;
    const env =
      Math.sin(Math.min(1, segProgress * 8) * (Math.PI / 2)) *
      Math.exp(-segProgress * 2.2) *
      0.24;
    const sample =
      env *
      (0.72 * Math.sin(2 * Math.PI * freq * t) +
        0.28 * Math.sin(2 * Math.PI * freq * 2 * t));
    const pcm16 = Math.max(-32767, Math.min(32767, Math.round(sample * 32767)));
    view.setInt16(44 + i * 2, pcm16, true);
  }

  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunkSize = 8192;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    const slice = bytes.subarray(offset, offset + chunkSize);
    binary += String.fromCharCode(...slice);
  }
  return `data:audio/wav;base64,${btoa(binary)}`;
}
