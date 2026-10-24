import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  buildRelativeRehearsalWsUrl,
  createSyntheticS16leChunkBase64,
  probeLiveBackendHealth,
} from "./api/liveSession";
import { createPollyFixtureWavDataUri } from "./audio/pollyPreview";
import {
  buildHapticRecord,
  CANONICAL_EXAMPLE_ATTEMPT,
  evaluateDutchTurnLocally,
  redactDeterministicPii,
  SCENARIOS,
  type BreakdownSummaryView,
  type CalendarRehearsalScenario,
  type CommittedTurnLog,
  type HapticEventRecord,
  type SituationId,
} from "./data/scenarios";

type StepStage = "calendar" | "rehearsal" | "breakdown";
type RuntimeMode = "sandbox" | "live";

interface WireEventEntry {
  id: string;
  direction: "client" | "server";
  eventType: string;
  detail: string;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

export function App(): React.ReactElement {
  const [selectedSituation, setSelectedSituation] = useState<SituationId>("shift_clarification");
  const [stage, setStage] = useState<StepStage>("calendar");
  const [runtimeMode, setRuntimeMode] = useState<RuntimeMode>("sandbox");
  const [liveAvailable, setLiveAvailable] = useState<boolean>(false);
  const [hapticsEnabled, setHapticsEnabled] = useState<boolean>(true);
  const [showEnglishSupport, setShowEnglishSupport] = useState<boolean>(true);
  const [showSupportCard, setShowSupportCard] = useState<boolean>(false);

  const [rehearsalId, setRehearsalId] = useState<string>("reh_sandbox_standup_01");
  const [turnIndex, setTurnIndex] = useState<0 | 1>(0);
  const [isStreamingSpeech, setIsStreamingSpeech] = useState<boolean>(false);
  const [streamingPartialText, setStreamingPartialText] = useState<string>("");
  const [streamingSeqId, setStreamingSeqId] = useState<number>(0);
  const [pcmDeletedConfirmed, setPcmDeletedConfirmed] = useState<boolean>(false);
  const [activeRepairHint, setActiveRepairHint] = useState<string | null>(null);
  const [repairCount, setRepairCount] = useState<number>(0);
  const [typedInput, setTypedInput] = useState<string>("");

  const [audioPlaying, setAudioPlaying] = useState<boolean>(false);
  const [audioProgressPct, setAudioProgressPct] = useState<number>(0);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const [latestHaptic, setLatestHaptic] = useState<HapticEventRecord | null>(null);
  const [hapticFlash, setHapticFlash] = useState<boolean>(false);
  const [committedTurns, setCommittedTurns] = useState<CommittedTurnLog[]>([]);
  const [breakdownSummary, setBreakdownSummary] = useState<BreakdownSummaryView | null>(null);
  const [wireEvents, setWireEvents] = useState<WireEventEntry[]>([]);

  const wsRef = useRef<WebSocket | null>(null);

  const scenario: CalendarRehearsalScenario = useMemo(() => {
    return (
      SCENARIOS.find((item) => item.situation === selectedSituation) ??
      (SCENARIOS[0] as CalendarRehearsalScenario)
    );
  }, [selectedSituation]);

  const currentTurn = scenario.turns[turnIndex];

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const forced = params.get("mode");
    void probeLiveBackendHealth().then((health) => {
      setLiveAvailable(health.reachable);
      if (forced === "sandbox") {
        setRuntimeMode("sandbox");
      } else if (forced === "live" && health.reachable) {
        setRuntimeMode("live");
      } else if (health.reachable) {
        setRuntimeMode("live");
      } else {
        setRuntimeMode("sandbox");
      }
    });
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, []);

  const appendWireEvent = (
    direction: "client" | "server",
    eventType: string,
    detail: string
  ): void => {
    setWireEvents((prev) => [
      {
        id: `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
        direction,
        eventType,
        detail,
      },
      ...prev.slice(0, 17),
    ]);
  };

  const triggerHapticDisplay = (record: HapticEventRecord): void => {
    setLatestHaptic(record);
    setHapticFlash(true);
    window.setTimeout(() => setHapticFlash(false), 650);
  };

  const playCounterpartPollyPreview = async (): Promise<void> => {
    if (audioPlaying) {
      return;
    }
    const wavUri = createPollyFixtureWavDataUri(
      `${scenario.situation}:${currentTurn.turnId}:${currentTurn.counterpartTextNl}`
    );
    setAudioPlaying(true);
    setAudioProgressPct(12);

    try {
      const audio = new Audio(wavUri);
      audioRef.current = audio;
      void audio.play().catch(() => undefined);
    } catch {
      // Headless or restricted autoplay environments still advance visual playback cleanly
    }

    const steps = [28, 48, 68, 86, 100];
    for (const pct of steps) {
      await sleep(210);
      setAudioProgressPct(pct);
    }
    await sleep(160);
    setAudioPlaying(false);
  };

  const buildFinalBreakdownSummary = (
    rehId: string,
    turns: readonly CommittedTurnLog[],
    repairs: number,
    serverSummary?: Record<string, unknown>
  ): BreakdownSummaryView => {
    const firstTurn = turns[0];
    const secondTurn = turns[1] ?? turns[0];
    const req0 = scenario.requiredCriteria[0];
    const req1 = scenario.requiredCriteria[1];

    const serverModelVersion =
      typeof serverSummary?.["model_version"] === "string"
        ? serverSummary["model_version"]
        : "deterministic-breakdown-v1";
    const serverFallback =
      typeof serverSummary?.["fallback_used"] === "boolean"
        ? serverSummary["fallback_used"]
        : true;

    const pacingNotes: string[] = [
      `Completed ${turns.length} bounded Dutch turns within the ${scenario.estimatedSeconds}s pre-moment rehearsal window.`,
      repairs > 0
        ? `Triggered ${repairs} inline functional repair prompt after an incomplete confirmation ('${scenario.priorAttemptSummary.priorSelfExcerpt}') and recovered with an explicit time/next-step confirmation.`
        : "Both expected communicative functions were satisfied without mid-turn pacing interruptions.",
      "Turn-boundary haptics fired only at counterpart readiness, PTT start, and PTT release — never continuously during speech.",
    ];

    return {
      rehearsalId: rehId,
      situation: scenario.situation,
      targetFunction: scenario.targetSkill,
      state: "completed",
      committedTurnCount: turns.length,
      repairPromptCount: repairs,
      minimizedExcerpts: turns.map((t) => ({
        text: t.minimizedTranscript,
        confidence: t.confidence,
      })),
      criteria: [
        {
          criterionId: req0,
          result: "satisfied",
          evidenceExcerpt: firstTurn?.minimizedTranscript ?? "",
          confidence: 0.91,
        },
        {
          criterionId: req1,
          result: "satisfied",
          evidenceExcerpt: secondTurn?.minimizedTranscript ?? "",
          confidence: 0.92,
        },
      ],
      breakdowns:
        repairs > 0
          ? [
              {
                code: "confirmation_missing_recovered_in_practice",
                description:
                  "Initial brief acknowledgement omitted explicit time/step confirmation; learner resolved it on the repair turn.",
                excerpt: secondTurn?.minimizedTranscript ?? "",
                confidence: 0.88,
              },
            ]
          : [],
      pacingNotes,
      modelVersion: serverModelVersion,
      fallbackUsed: serverFallback,
      otherSpeakerTranscriptRetained: false,
      rawPcmRetainedBytes: 0,
      authenticMasteryUpdated: false,
    };
  };

  const startRehearsalFromCandidate = async (): Promise<void> => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setCommittedTurns([]);
    setBreakdownSummary(null);
    setTurnIndex(0);
    setStreamingPartialText("");
    setStreamingSeqId(0);
    setPcmDeletedConfirmed(false);
    setActiveRepairHint(null);
    setRepairCount(0);
    setTypedInput("");
    setShowSupportCard(false);
    setAudioProgressPct(0);
    setWireEvents([]);

    if (runtimeMode === "live" && liveAvailable) {
      try {
        const wsUrl = buildRelativeRehearsalWsUrl({
          situation: scenario.situation,
          hapticsEnabled,
          learnerId: CANONICAL_EXAMPLE_ATTEMPT.learner_id,
        });
        const ws = new WebSocket(wsUrl);
        wsRef.current = ws;

        await new Promise<void>((resolve, reject) => {
          const timeout = window.setTimeout(() => reject(new Error("ws_timeout")), 2200);
          ws.onopen = () => {
            window.clearTimeout(timeout);
            resolve();
          };
          ws.onerror = () => {
            window.clearTimeout(timeout);
            reject(new Error("ws_error"));
          };
        });

        ws.onmessage = (ev: MessageEvent<string>) => {
          try {
            const msg = JSON.parse(ev.data) as Record<string, unknown>;
            const msgType = String(msg["type"] ?? "unknown");
            if (msgType === "session_ready") {
              const id = String(msg["rehearsal_id"] ?? "reh_live");
              setRehearsalId(id);
              appendWireEvent("server", "session_ready", `rehearsal_id=${id}`);
            } else if (msgType === "counterpart_audio_ready") {
              appendWireEvent(
                "server",
                "counterpart_audio_ready",
                `turn=${String(msg["turn_id"])} persona=${String(msg["counterpart_persona"])}`
              );
            } else if (msgType === "turn_haptic") {
              const trig = String(msg["trigger"] ?? "turn_boundary");
              const pat = (msg["pattern"] as "SHORT_DOUBLE" | "STEADY_BUZZ" | null) ?? null;
              const rec = buildHapticRecord(trig, Boolean(msg["emitted"]));
              triggerHapticDisplay({
                ...rec,
                pattern: pat,
              });
              appendWireEvent(
                "server",
                "turn_haptic",
                `pattern=${String(pat ?? "SUPPRESSED")} trigger=${trig}`
              );
            } else if (msgType === "transcription_update") {
              const isPartial = Boolean(msg["is_partial"]);
              appendWireEvent(
                "server",
                "transcription_update",
                `seq=${String(msg["sequence_id"])} partial=${String(isPartial)} pcm_retained=${String(msg["retained_pcm_bytes"])}B`
              );
            } else if (msgType === "breakdown_summary") {
              appendWireEvent(
                "server",
                "breakdown_summary",
                `model=${String(msg["model_version"])} pcm_retained=${String(msg["raw_pcm_retained_bytes"])}B`
              );
            }
          } catch {
            // ignore non-json
          }
        };

        ws.send(JSON.stringify({ type: "request_counterpart", request_id: "cp_init_t1" }));
        appendWireEvent("client", "request_counterpart", "turn=t1");
        setStage("rehearsal");
        return;
      } catch {
        wsRef.current = null;
      }
    }

    // Deterministic Sandbox Mode (works identically on static GitHub Pages without Python)
    const sandboxId = `reh_${scenario.situation.slice(0, 6)}_sandbox`;
    setRehearsalId(sandboxId);
    appendWireEvent("server", "session_ready", `rehearsal_id=${sandboxId} mode=sandbox`);
    appendWireEvent("client", "request_counterpart", "turn=t1");
    appendWireEvent(
      "server",
      "counterpart_audio_ready",
      `turn=t1 persona=${scenario.turns[0].counterpartPersona}`
    );
    const hap = buildHapticRecord("counterpart_turn_ready", hapticsEnabled);
    triggerHapticDisplay(hap);
    appendWireEvent(
      "server",
      "turn_haptic",
      `pattern=${String(hap.pattern ?? "SUPPRESSED")} trigger=counterpart_turn_ready`
    );
    setStage("rehearsal");
  };

  const commitEvaluatedTurn = async (params: {
    rawTranscript: string;
    mode: "speech" | "tap" | "text";
    supportPhraseId?: string;
    confidence: number;
    durationMs: number;
  }): Promise<void> => {
    const { text: scrubbed, redacted } = redactDeterministicPii(params.rawTranscript);
    const evalResult = evaluateDutchTurnLocally(
      currentTurn,
      scrubbed,
      params.supportPhraseId
    );

    if (!evalResult.satisfied) {
      const nextRepairs = repairCount + 1;
      setRepairCount(nextRepairs);
      setActiveRepairHint(currentTurn.repairGuidanceEn);

      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "phrase_attempt",
            turn_event_id: `miss_${currentTurn.turnId}_${nextRepairs}`,
            transcript: scrubbed,
            mode: params.mode,
            confidence: params.confidence,
          })
        );
        appendWireEvent("client", "phrase_attempt", `"${scrubbed}"`);
      } else {
        appendWireEvent("client", "phrase_attempt", `"${scrubbed}"`);
        const hap = buildHapticRecord("support_repair_offered", hapticsEnabled);
        triggerHapticDisplay(hap);
        appendWireEvent(
          "server",
          "transcription_update",
          `support=repair mapped=null pcm_retained=0B`
        );
        appendWireEvent(
          "server",
          "turn_haptic",
          `pattern=${String(hap.pattern ?? "SUPPRESSED")} trigger=support_repair_offered`
        );
      }
      return;
    }

    setActiveRepairHint(null);
    const entry: CommittedTurnLog = {
      turnId: currentTurn.turnId,
      mode: params.mode,
      minimizedTranscript: scrubbed,
      redacted,
      mappedFunction: evalResult.mappedFunction,
      supportOffered: null,
      confidence: params.confidence,
      retainedPcmBytes: 0,
      turnDurationMs: params.durationMs,
    };
    const updatedTurns = [...committedTurns, entry];
    setCommittedTurns(updatedTurns);

    if (turnIndex === 0) {
      setTurnIndex(1);
      setStreamingPartialText("");
      setShowSupportCard(false);
      setAudioProgressPct(0);

      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({ type: "request_counterpart", request_id: "cp_t2" })
        );
        appendWireEvent("client", "request_counterpart", "turn=t2");
      } else {
        const boundaryHap = buildHapticRecord("turn_budget_boundary", hapticsEnabled);
        triggerHapticDisplay(boundaryHap);
        appendWireEvent(
          "server",
          "turn_haptic",
          `pattern=${String(boundaryHap.pattern ?? "SUPPRESSED")} trigger=turn_budget_boundary`
        );
        await sleep(240);
        appendWireEvent(
          "server",
          "counterpart_audio_ready",
          `turn=t2 persona=${scenario.turns[1].counterpartPersona}`
        );
        const readyHap = buildHapticRecord("counterpart_turn_ready", hapticsEnabled);
        triggerHapticDisplay(readyHap);
      }
      return;
    }

    // Completed final turn -> build post-rehearsal communication breakdown summary
    const doneHap = buildHapticRecord("rehearsal_completed", hapticsEnabled);
    triggerHapticDisplay(doneHap);
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      appendWireEvent(
        "server",
        "turn_haptic",
        `pattern=${String(doneHap.pattern ?? "SUPPRESSED")} trigger=rehearsal_completed`
      );
      appendWireEvent(
        "server",
        "breakdown_summary",
        "other_speaker_retained=false raw_pcm_retained_bytes=0 authentic_mastery_updated=false"
      );
    }
    const summary = buildFinalBreakdownSummary(rehearsalId, updatedTurns, repairCount);
    setBreakdownSummary(summary);
    setStage("breakdown");
  };

  const simulateConsentedSpeechTurn = async (): Promise<void> => {
    if (isStreamingSpeech) {
      return;
    }
    setIsStreamingSpeech(true);
    setPcmDeletedConfirmed(false);
    setActiveRepairHint(null);

    const turnEventId = `ptt_${currentTurn.turnId}_${Date.now()}`;
    const pcmChunk = createSyntheticS16leChunkBase64(800);
    const chunks = currentTurn.streamingChunks;

    // PTT press haptic (STEADY_BUZZ) — never repeated mid-recording (ui/DESIGN.md §6)
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      const startHap = buildHapticRecord("ptt_recording_started", hapticsEnabled);
      triggerHapticDisplay(startHap);
      appendWireEvent(
        "server",
        "turn_haptic",
        `pattern=${String(startHap.pattern ?? "SUPPRESSED")} trigger=ptt_recording_started`
      );
    }

    for (let idx = 0; idx < chunks.length; idx += 1) {
      const seq = idx + 1;
      const isFinal = idx === chunks.length - 1;
      const chunkText = chunks[idx] ?? currentTurn.recommendedSpeechNl;
      setStreamingSeqId(seq);
      setStreamingPartialText(chunkText);

      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "audio_chunk",
            turn_event_id: turnEventId,
            sequence_id: seq,
            pcm_base64: pcmChunk,
            is_final: isFinal,
            streaming_transcription_consented: true,
            transcript_hint: isFinal ? currentTurn.recommendedSpeechNl : undefined,
          })
        );
        appendWireEvent(
          "client",
          "audio_chunk",
          `seq=${seq} final=${String(isFinal)} s16le_16khz`
        );
      } else {
        appendWireEvent(
          "client",
          "audio_chunk",
          `seq=${seq} final=${String(isFinal)} s16le_16khz`
        );
        appendWireEvent(
          "server",
          "transcription_update",
          `seq=${seq} partial=${String(!isFinal)} pcm_retained=${isFinal ? 0 : 1600}B`
        );
      }

      await sleep(isFinal ? 320 : 420);
    }

    setIsStreamingSpeech(false);
    setPcmDeletedConfirmed(true);

    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      const relHap = buildHapticRecord("ptt_recording_released", hapticsEnabled);
      triggerHapticDisplay(relHap);
      appendWireEvent(
        "server",
        "turn_haptic",
        `pattern=${String(relHap.pattern ?? "SUPPRESSED")} trigger=ptt_recording_released`
      );
    }

    await sleep(260);
    await commitEvaluatedTurn({
      rawTranscript: currentTurn.recommendedSpeechNl,
      mode: "speech",
      confidence: 0.94,
      durationMs: chunks.length * 420,
    });
  };

  return (
    <div className="nt-shell">
      <div className="nt-provenance-banner" data-testid="provenance-banner">
        <div>
          <strong>
            {runtimeMode === "live" && liveAvailable
              ? "LIVE LOCAL API + FIXTURE PROVIDERS"
              : "SANDBOX / FIXTURE RENDITION"}
          </strong>{" "}
          · Seeded from <code>examples/real-world-attempt.example.json</code> (
          <code>{CANONICAL_EXAMPLE_ATTEMPT.source.bee_recording_reference}</code>). Practice never
          updates authentic real-world skill mastery (<code>AC-NT-015</code>).
        </div>
        <div className="nt-toolbar-controls">
          <span
            className={`nt-pill ${
              runtimeMode === "live" && liveAvailable ? "nt-pill--success" : "nt-pill--terracotta"
            }`}
            data-testid="runtime-mode-pill"
          >
            {runtimeMode === "live" && liveAvailable
              ? "Connected · /v1/rehearsal/ws"
              : "Standalone Sandbox · No Python Required"}
          </span>
          {liveAvailable && (
            <button
              type="button"
              className="nt-btn nt-btn--secondary"
              style={{ minHeight: 34, padding: "4px 10px", fontSize: 13 }}
              onClick={() =>
                setRuntimeMode((prev) => (prev === "live" ? "sandbox" : "live"))
              }
            >
              Switch to {runtimeMode === "live" ? "Sandbox" : "Live API"}
            </button>
          )}
        </div>
      </div>

      <header className="nt-topbar">
        <div>
          <span className="nt-brand-eyebrow">NextTime · Quiet Pocket Coach · Dutch (A2–B1)</span>
          <h1 className="nt-title">Rehearse just before the next real moment.</h1>
          <p className="nt-subtitle">
            User-confirmed calendar candidates, bounded 90-second Dutch push-to-talk practice with
            immediate PCM deletion, turn-boundary haptics, and post-rehearsal breakdown diagnostics.
          </p>
        </div>
        <div className="nt-toolbar-controls">
          <button
            type="button"
            className="nt-btn nt-btn--secondary"
            data-testid="toggle-haptics-btn"
            onClick={() => setHapticsEnabled((prev) => !prev)}
          >
            {hapticsEnabled ? "Haptics: On (Turn-Boundary)" : "Haptics: Suppressed (Captions)"}
          </button>
        </div>
      </header>

      <nav className="nt-stepper" aria-label="Rehearsal flow steps">
        <button
          type="button"
          className={`nt-step-btn ${stage === "calendar" ? "nt-step-btn--active" : ""}`}
          data-testid="step-tab-calendar"
          onClick={() => setStage("calendar")}
        >
          <span className="nt-step-num">1</span>
          <span>Calendar Candidate &amp; Prior Attempt</span>
        </button>
        <button
          type="button"
          className={`nt-step-btn ${stage === "rehearsal" ? "nt-step-btn--active" : ""}`}
          data-testid="step-tab-rehearsal"
          disabled={stage === "calendar" && committedTurns.length === 0 && !latestHaptic}
          onClick={() => setStage("rehearsal")}
        >
          <span className="nt-step-num">2</span>
          <span>90s Dutch Rehearsal Player</span>
        </button>
        <button
          type="button"
          className={`nt-step-btn ${stage === "breakdown" ? "nt-step-btn--active" : ""}`}
          data-testid="step-tab-breakdown"
          disabled={!breakdownSummary}
          onClick={() => setStage("breakdown")}
        >
          <span className="nt-step-num">3</span>
          <span>Post-Rehearsal Breakdown</span>
        </button>
      </nav>

      <main className="nt-grid">
        <div>
          {stage === "calendar" && (
            <section className="nt-card nt-card--selected" data-testid="calendar-candidate-panel">
              <div className="nt-row-between">
                <span className="nt-brand-eyebrow">NT-NextMoment · Explicit Learner Confirmation</span>
                <span className="nt-pill nt-pill--moss">Raw Calendar Titles Never Retained</span>
              </div>
              <h2 className="nt-section-title">Select Upcoming Dutch Moment</h2>
              <p className="nt-muted">
                Calendar sync matches only the three supported non-sensitive Dutch situations and
                stores opaque <code>calref_*</code> digests until you explicitly confirm a 90-second
                rehearsal.
              </p>

              <div className="nt-candidate-list" role="radiogroup" aria-label="Dutch situations">
                {SCENARIOS.map((item) => {
                  const selected = item.situation === selectedSituation;
                  return (
                    <button
                      key={item.situation}
                      type="button"
                      role="radio"
                      aria-checked={selected}
                      data-testid={`candidate-${item.situation}`}
                      className={`nt-candidate-item ${
                        selected ? "nt-candidate-item--selected" : ""
                      }`}
                      onClick={() => setSelectedSituation(item.situation)}
                    >
                      <div className="nt-row-between">
                        <strong>{item.title}</strong>
                        <span className="nt-pill nt-pill--info tabular">
                          {item.estimatedSeconds}s rehearsal
                        </span>
                      </div>
                      <p className="nt-muted" style={{ marginTop: 4 }}>
                        {item.dutchLabel} · {item.eventWindowLocal}
                      </p>
                      <div
                        className="nt-row-between"
                        style={{ marginTop: 8, fontSize: 13, color: "var(--nt-text-muted)" }}
                      >
                        <span>
                          Source: <code>{item.calendarProvider}</code> (
                          <code>{item.calendarOpaqueRef}</code>)
                        </span>
                        <span>
                          Focus: <strong>{item.targetSkill}</strong>
                        </span>
                      </div>
                    </button>
                  );
                })}
              </div>

              <div style={{ marginTop: 18 }}>
                <button
                  type="button"
                  className="nt-btn nt-btn--primary nt-btn--full"
                  data-testid="confirm-candidate-btn"
                  onClick={() => void startRehearsalFromCandidate()}
                >
                  Confirm Candidate &amp; Start 90-Second Dutch Rehearsal
                </button>
              </div>
            </section>
          )}

          {stage === "rehearsal" && (
            <section className="nt-card nt-card--selected" data-testid="rehearsal-player-panel">
              <div className="nt-row-between">
                <span className="nt-brand-eyebrow">
                  NT-RehearsalPlayer · Turn {turnIndex + 1} of {scenario.turns.length} ·{" "}
                  {scenario.dutchLabel}
                </span>
                <span className="nt-pill nt-pill--moss tabular">
                  90s budget · Target: {currentTurn.expectedFunction}
                </span>
              </div>

              <div className="nt-card nt-card--alt" style={{ marginTop: 12 }}>
                <div className="nt-row-between">
                  <span className="nt-brand-eyebrow">
                    Counterpart ({currentTurn.counterpartPersona})
                  </span>
                  <span className="nt-pill">{currentTurn.pollyVoiceLabel}</span>
                </div>
                <p className="nt-counterpart-nl" data-testid="counterpart-text-nl">
                  “{currentTurn.counterpartTextNl}”
                </p>
                {showEnglishSupport && (
                  <p className="nt-counterpart-en" data-testid="counterpart-text-en">
                    {currentTurn.counterpartTextEn}
                  </p>
                )}

                <div className="nt-audio-bar">
                  <button
                    type="button"
                    className="nt-btn nt-btn--secondary"
                    style={{ minHeight: 38, padding: "6px 12px", fontSize: 14 }}
                    data-testid="play-polly-audio-btn"
                    onClick={() => void playCounterpartPollyPreview()}
                  >
                    {audioPlaying ? "Playing Counterpart…" : "Play Polly Counterpart Audio"}
                  </button>
                  <div className="nt-progress-track" aria-label="Counterpart audio progress">
                    <div
                      className="nt-progress-fill"
                      data-testid="polly-audio-progress"
                      style={{ width: `${audioProgressPct}%` }}
                    />
                  </div>
                  <button
                    type="button"
                    className="nt-btn nt-btn--secondary"
                    style={{ minHeight: 38, padding: "6px 10px", fontSize: 13 }}
                    onClick={() => setShowEnglishSupport((prev) => !prev)}
                  >
                    {showEnglishSupport ? "Hide English" : "Show English"}
                  </button>
                </div>
              </div>

              <div className="nt-transcript-box" data-testid="streaming-transcript-box">
                <div className="nt-row-between">
                  <strong>Learner Turn · Consented Push-to-Talk</strong>
                  {isStreamingSpeech ? (
                    <span className="nt-recording-badge" data-testid="recording-active-badge">
                      <span className="nt-record-dot" aria-hidden="true" />
                      Recording · Chunk #{streamingSeqId}
                    </span>
                  ) : pcmDeletedConfirmed ? (
                    <span
                      className="nt-pill nt-pill--success tabular"
                      data-testid="pcm-deleted-badge"
                    >
                      Raw PCM Deleted · 0 bytes retained (FR-NT-014)
                    </span>
                  ) : (
                    <span className="nt-pill">Intentional turn only · No ambient mic</span>
                  )}
                </div>

                <p
                  style={{
                    margin: "10px 0 4px",
                    fontSize: 17,
                    fontWeight: streamingPartialText ? 600 : 400,
                    color: streamingPartialText ? "var(--nt-text)" : "var(--nt-text-muted)",
                  }}
                  data-testid="streaming-transcript-text"
                >
                  {streamingPartialText
                    ? `“${streamingPartialText}”`
                    : "Press Simulate Consented Dutch Speech (PTT) or choose a non-speaking option below."}
                </p>

                {activeRepairHint && (
                  <div
                    className="nt-repair-callout"
                    role="status"
                    aria-live="polite"
                    data-testid="repair-coaching-callout"
                  >
                    <strong>Gentle Functional Repair ({currentTurn.expectedFunction}):</strong>{" "}
                    {activeRepairHint}
                  </div>
                )}
              </div>

              <div className="nt-phrase-grid">
                <button
                  type="button"
                  className="nt-btn nt-btn--primary nt-btn--full"
                  disabled={isStreamingSpeech}
                  data-testid="simulate-speech-ptt-btn"
                  onClick={() => void simulateConsentedSpeechTurn()}
                >
                  {isStreamingSpeech
                    ? "Streaming Dutch Speech Chunks…"
                    : `Simulate Consented Dutch Speech (PTT): “${currentTurn.recommendedSpeechNl}”`}
                </button>

                <div className="nt-row-between" style={{ gap: 10 }}>
                  <button
                    type="button"
                    className="nt-btn nt-btn--secondary"
                    style={{ flex: 1 }}
                    disabled={isStreamingSpeech}
                    data-testid="try-ambiguous-reply-btn"
                    onClick={() =>
                      void commitEvaluatedTurn({
                        rawTranscript: currentTurn.ambiguousExampleNl,
                        mode: "speech",
                        confidence: 0.86,
                        durationMs: 620,
                      })
                    }
                  >
                    Try Incomplete Reply (“{currentTurn.ambiguousExampleNl}”)
                  </button>

                  <button
                    type="button"
                    className="nt-btn nt-btn--secondary"
                    style={{ flex: 1 }}
                    disabled={isStreamingSpeech}
                    data-testid="toggle-support-phrase-btn"
                    onClick={() => setShowSupportCard((prev) => !prev)}
                  >
                    {showSupportCard ? "Hide Support Phrase" : "Need a Dutch Support Phrase?"}
                  </button>
                </div>

                {showSupportCard && (
                  <div className="nt-card nt-card--alt" data-testid="support-phrase-card">
                    <div className="nt-row-between">
                      <strong>{currentTurn.supportPhraseNl}</strong>
                      <span className="nt-pill">Non-speaking practice</span>
                    </div>
                    <p className="nt-muted" style={{ margin: "4px 0 10px" }}>
                      {currentTurn.supportPhraseEn}
                    </p>
                    <button
                      type="button"
                      className="nt-btn nt-btn--secondary nt-btn--full"
                      data-testid="tap-support-phrase-btn"
                      onClick={() =>
                        void commitEvaluatedTurn({
                          rawTranscript: currentTurn.supportPhraseNl,
                          mode: "tap",
                          supportPhraseId: currentTurn.supportPhraseId,
                          confidence: 0.95,
                          durationMs: 400,
                        })
                      }
                    >
                      Complete Turn with Support Phrase Tap
                    </button>
                  </div>
                )}

                <form
                  className="nt-input-row"
                  onSubmit={(ev) => {
                    ev.preventDefault();
                    if (!typedInput.trim()) {
                      return;
                    }
                    const text = typedInput.trim();
                    setTypedInput("");
                    void commitEvaluatedTurn({
                      rawTranscript: text,
                      mode: "text",
                      confidence: 0.95,
                      durationMs: 500,
                    });
                  }}
                >
                  <input
                    type="text"
                    className="nt-text-input"
                    placeholder="Or type Dutch response (non-speaking practice)…"
                    aria-label="Type Dutch response"
                    data-testid="typed-dutch-input"
                    value={typedInput}
                    onChange={(ev) => setTypedInput(ev.target.value)}
                  />
                  <button
                    type="button"
                    className="nt-btn nt-btn--secondary"
                    data-testid="submit-typed-dutch-btn"
                    disabled={!typedInput.trim() || isStreamingSpeech}
                    onClick={() => {
                      const text = typedInput.trim();
                      if (!text) {
                        return;
                      }
                      setTypedInput("");
                      void commitEvaluatedTurn({
                        rawTranscript: text,
                        mode: "text",
                        confidence: 0.95,
                        durationMs: 500,
                      });
                    }}
                  >
                    Send Text
                  </button>
                </form>
              </div>
            </section>
          )}

          {stage === "breakdown" && breakdownSummary && (
            <section className="nt-card nt-card--selected" data-testid="breakdown-summary-panel">
              <div className="nt-row-between">
                <span className="nt-brand-eyebrow">
                  Post-Rehearsal Communication &amp; Pacing Breakdown
                </span>
                <span className="nt-pill nt-pill--moss" data-testid="practice-completed-badge">
                  Practice completed (Rehearsal ≠ Authentic Mastery)
                </span>
              </div>
              <h2 className="nt-section-title" style={{ marginTop: 6 }}>
                {scenario.title} · Diagnostic Summary
              </h2>
              <p className="nt-muted">
                Evaluated minimized learner-only excerpts against <code>rubric_v1</code> via{" "}
                <code>{breakdownSummary.modelVersion}</code>. Counterpart speech was discarded and
                raw PCM audio was zeroed on every turn boundary.
              </p>

              <div className="nt-card nt-card--alt" style={{ marginTop: 14 }}>
                <strong>Functional Rubric Criteria (rubric_v1)</strong>
                <ul className="nt-kv-list" data-testid="breakdown-criteria-list">
                  {breakdownSummary.criteria.map((crit) => (
                    <li key={crit.criterionId} className="nt-kv-item">
                      <div>
                        <code>{crit.criterionId}</code>
                        <div className="nt-muted" style={{ fontSize: 13 }}>
                          Self excerpt: “{crit.evidenceExcerpt}”
                        </div>
                      </div>
                      <span className="nt-pill nt-pill--success tabular">
                        {crit.result} · conf {crit.confidence.toFixed(2)}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>

              <div className="nt-card" style={{ marginTop: 14 }}>
                <strong>Turn Pacing &amp; Repair Breakdown Analysis</strong>
                <ul className="nt-kv-list" data-testid="pacing-notes-list">
                  {breakdownSummary.pacingNotes.map((note, idx) => (
                    <li key={idx} className="nt-kv-item">
                      <span>{note}</span>
                    </li>
                  ))}
                </ul>
              </div>

              <div className="nt-card" style={{ marginTop: 14 }}>
                <strong>Privacy &amp; Boundary Verification Ledger</strong>
                <ul className="nt-kv-list" data-testid="privacy-ledger-list">
                  <li className="nt-kv-item">
                    <span>Other-speaker transcript retained (SEC-NT-002)</span>
                    <code data-testid="ledger-other-speaker">false</code>
                  </li>
                  <li className="nt-kv-item">
                    <span>Raw PCM audio bytes retained (FR-NT-014)</span>
                    <code className="tabular" data-testid="ledger-pcm-bytes">
                      0 bytes
                    </code>
                  </li>
                  <li className="nt-kv-item">
                    <span>Authentic skill mastery mutated by rehearsal (AC-NT-015)</span>
                    <code data-testid="ledger-authentic-mastery">false</code>
                  </li>
                </ul>
              </div>

              <div style={{ display: "flex", gap: 10, marginTop: 16 }}>
                <button
                  type="button"
                  className="nt-btn nt-btn--primary"
                  data-testid="practice-again-btn"
                  onClick={() => void startRehearsalFromCandidate()}
                >
                  Practice Scenario Again
                </button>
                <button
                  type="button"
                  className="nt-btn nt-btn--secondary"
                  data-testid="choose-another-scenario-btn"
                  onClick={() => setStage("calendar")}
                >
                  Choose Another Dutch Situation
                </button>
              </div>
            </section>
          )}
        </div>

        <aside>
          <section
            className={`nt-card nt-haptic-box ${hapticFlash ? "nt-haptic-box--active" : ""}`}
            aria-live="polite"
            data-testid="haptic-telemetry-panel"
          >
            <div className="nt-row-between">
              <span className="nt-brand-eyebrow">PacingHapticsEngine · Turn-Boundary</span>
              <span className="nt-pill">Bee Wearable Excluded</span>
            </div>
            {latestHaptic ? (
              <div style={{ marginTop: 8 }}>
                <div className="nt-row-between">
                  <span className="nt-haptic-glyph" data-testid="haptic-pattern-badge">
                    {latestHaptic.pattern === "SHORT_DOUBLE"
                      ? "●━● SHORT_DOUBLE"
                      : latestHaptic.pattern === "STEADY_BUZZ"
                        ? "━━━●━━━ STEADY_BUZZ"
                        : "○ SUPPRESSED"}
                  </span>
                  <span className="nt-muted tabular" style={{ fontSize: 13 }}>
                    {latestHaptic.timingLabel}
                  </span>
                </div>
                <p style={{ margin: "8px 0 2px", fontSize: 14, fontWeight: 600 }}>
                  Trigger: <code>{latestHaptic.trigger}</code>
                </p>
                <p className="nt-muted" style={{ fontSize: 13.5 }}>
                  {latestHaptic.voiceoverAnnouncement}
                </p>
              </div>
            ) : (
              <p className="nt-muted" style={{ marginTop: 8 }}>
                Confirm a calendar candidate to initialize turn-boundary haptic cues (
                <code>SHORT_DOUBLE</code> &amp; <code>STEADY_BUZZ</code>).
              </p>
            )}
          </section>

          <section className="nt-card" data-testid="prior-attempt-context-card">
            <span className="nt-brand-eyebrow">
              Prior Consented Bee Attempt (real-world-attempt.example.json)
            </span>
            <h3 style={{ margin: "4px 0 6px", fontSize: 17 }}>
              Proposed Breakdown: <code>{scenario.priorAttemptSummary.breakdownCode}</code>
            </h3>
            <p className="nt-muted">{scenario.priorAttemptSummary.breakdownDescription}</p>
            <blockquote className="nt-quote">
              <div style={{ fontWeight: 600 }}>
                “{scenario.priorAttemptSummary.priorSelfExcerpt}”
              </div>
              <div className="nt-muted tabular" style={{ fontSize: 12.5, marginTop: 4 }}>
                Learner self-evidence · conf {scenario.priorAttemptSummary.confidence.toFixed(2)} ·
                Other speaker retained: <code>false</code>
              </div>
            </blockquote>
          </section>

          <section className="nt-card" data-testid="wire-protocol-log">
            <div className="nt-row-between">
              <span className="nt-brand-eyebrow">/v1/rehearsal/ws Wire Telemetry</span>
              <span className="nt-muted tabular" style={{ fontSize: 12 }}>
                {wireEvents.length} events
              </span>
            </div>
            <div className="nt-log-feed">
              {wireEvents.length === 0 ? (
                <p className="nt-muted" style={{ fontSize: 13 }}>
                  Streaming events (<code>audio_chunk</code>, <code>transcription_update</code>,{" "}
                  <code>turn_haptic</code>, <code>breakdown_summary</code>) appear here.
                </p>
              ) : (
                wireEvents.map((item) => (
                  <div key={item.id} className="nt-log-entry">
                    <div>
                      <strong>{item.direction === "client" ? "Client →" : "← Server"}</strong>{" "}
                      <code>{item.eventType}</code>
                    </div>
                    <div className="nt-muted tabular" style={{ fontSize: 12.5 }}>
                      {item.detail}
                    </div>
                  </div>
                ))
              )}
            </div>
          </section>
        </aside>
      </main>
    </div>
  );
}
