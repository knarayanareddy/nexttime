import canonicalAttempt from "../../../../examples/real-world-attempt.example.json";

export type SituationId =
  | "shift_clarification"
  | "repair_appointment"
  | "school_admin_appointment";

export type HapticPattern = "SHORT_DOUBLE" | "STEADY_BUZZ" | null;

export interface HapticEventRecord {
  eventId: string;
  trigger: string;
  pattern: HapticPattern;
  timingLabel: string;
  emitted: boolean;
  suppressionReason: string | null;
  visualCaptionKey: string;
  voiceoverAnnouncement: string;
  targetDevice: "cli_rehearsal_simulator" | "ios_rehearsal_client";
}

export interface ScenarioTurnSpec {
  turnId: "t1" | "t2";
  counterpartPersona: string;
  pollyVoiceLabel: string;
  counterpartTextNl: string;
  counterpartTextEn: string;
  expectedFunction: string;
  signedAudioUrl: string;
  supportPhraseId: string;
  supportPhraseNl: string;
  supportPhraseEn: string;
  recommendedSpeechNl: string;
  recommendedSpeechEn: string;
  streamingChunks: readonly string[];
  ambiguousExampleNl: string;
  repairGuidanceEn: string;
}

export interface CalendarRehearsalScenario {
  situation: SituationId;
  title: string;
  dutchLabel: string;
  calendarOpaqueRef: string;
  calendarProvider: "icalendar" | "learner_marked";
  eventWindowLocal: string;
  rehearsalWindowLocal: string;
  timeZone: string;
  estimatedSeconds: number;
  targetSkill: string;
  requiredCriteria: readonly [string, string];
  priorAttemptSummary: {
    attemptId: string;
    beeReference: string;
    priorStatus: string;
    breakdownCode: string;
    breakdownDescription: string;
    priorSelfExcerpt: string;
    confidence: number;
  };
  turns: readonly [ScenarioTurnSpec, ScenarioTurnSpec];
}

export interface CommittedTurnLog {
  turnId: string;
  mode: "speech" | "tap" | "text";
  minimizedTranscript: string;
  redacted: boolean;
  mappedFunction: string | null;
  supportOffered: "repair" | "easier_alternative" | null;
  confidence: number;
  retainedPcmBytes: 0;
  turnDurationMs: number;
}

export interface BreakdownSummaryView {
  rehearsalId: string;
  situation: SituationId;
  targetFunction: string;
  state: "completed";
  committedTurnCount: number;
  repairPromptCount: number;
  minimizedExcerpts: readonly { text: string; confidence: number }[];
  criteria: readonly {
    criterionId: string;
    result: "satisfied" | "not_satisfied" | "unknown";
    evidenceExcerpt: string;
    confidence: number;
  }[];
  breakdowns: readonly {
    code: string;
    description: string;
    excerpt: string;
    confidence: number;
  }[];
  pacingNotes: readonly string[];
  modelVersion: string;
  fallbackUsed: boolean;
  otherSpeakerTranscriptRetained: false;
  rawPcmRetainedBytes: 0;
  authenticMasteryUpdated: false;
}

export const CANONICAL_EXAMPLE_ATTEMPT = canonicalAttempt;

const primaryBreakdown = canonicalAttempt.breakdowns[0] ?? {
  breakdown_id: "01000000000000000000000005",
  code: "confirmation_missing",
  description: "The attempt ended without an explicit confirmation of the proposed time.",
  self_evidence_excerpt: "Ja, dank u.",
  confidence: 0.82,
  review_status: "proposed",
};

export const SCENARIOS: readonly CalendarRehearsalScenario[] = [
  {
    situation: "shift_clarification",
    title: "Dutch Standup & Shift Handover",
    dutchLabel: "Dienstoverleg · Ochtend-standup",
    calendarOpaqueRef: "calref_8f41c209e7b14d02a9318c44",
    calendarProvider: "icalendar",
    eventWindowLocal: "Today · 09:00–09:15 CEST",
    rehearsalWindowLocal: "08:45 CEST (15 min before standup)",
    timeZone: canonicalAttempt.next_occurrence.time_zone,
    estimatedSeconds: 90,
    targetSkill: "confirmation",
    requiredCriteria: ["goal_stated", "next_step_confirmed"],
    priorAttemptSummary: {
      attemptId: canonicalAttempt.attempt_id,
      beeReference: canonicalAttempt.source.bee_recording_reference,
      priorStatus: canonicalAttempt.outcome.status,
      breakdownCode: primaryBreakdown.code,
      breakdownDescription:
        "Prior standup handover closed with polite acknowledgement ('Ja, dank u.') before restating the concrete shift start and next step.",
      priorSelfExcerpt: primaryBreakdown.self_evidence_excerpt,
      confidence: primaryBreakdown.confidence,
    },
    turns: [
      {
        turnId: "t1",
        counterpartPersona: "shift_scheduler",
        pollyVoiceLabel: "Amazon Polly · Lotte (nl-NL · fixture preview)",
        counterpartTextNl:
          "Goedemorgen bij de standup. Wat wilt u vandaag voor uw dienst afstemmen?",
        counterpartTextEn:
          "Good morning at the standup. What would you like to align for your shift today?",
        expectedFunction: "goal_stated",
        signedAudioUrl:
          "https://audio.nexttime.local/v1/counterpart-clips/standup-t1?expires=900s&mode=fixture",
        supportPhraseId: "goal_shift_basic",
        supportPhraseNl: "Ik wil mijn dienst en de volgende stap controleren.",
        supportPhraseEn: "I want to check my shift and the next step.",
        recommendedSpeechNl:
          "Goedemorgen, ik wil tijdens de standup mijn dienst en de volgende stap controleren.",
        recommendedSpeechEn:
          "Good morning, during the standup I want to check my shift and the next step.",
        streamingChunks: [
          "Goedemorgen, ik wil tijdens...",
          "Goedemorgen, ik wil tijdens de standup mijn dienst...",
          "Goedemorgen, ik wil tijdens de standup mijn dienst en de volgende stap controleren.",
        ],
        ambiguousExampleNl: "Goedemorgen allemaal.",
        repairGuidanceEn:
          "State your concrete standup goal in Dutch (e.g. checking your shift or next step) so your team lead knows what to confirm.",
      },
      {
        turnId: "t2",
        counterpartPersona: "shift_scheduler",
        pollyVoiceLabel: "Amazon Polly · Lotte (nl-NL · fixture preview)",
        counterpartTextNl:
          "Uw volgende dienst begint dinsdag om acht uur met de overdracht. Kunt u die volgende stap bevestigen?",
        counterpartTextEn:
          "Your next shift starts Tuesday at 08:00 with the handover. Can you confirm that next step?",
        expectedFunction: "next_step_confirmed",
        signedAudioUrl:
          "https://audio.nexttime.local/v1/counterpart-clips/standup-t2?expires=900s&mode=fixture",
        supportPhraseId: "confirm_time",
        supportPhraseNl: "Ja, ik bevestig de datum en de volgende stap.",
        supportPhraseEn: "Yes, I confirm the date and the next step.",
        recommendedSpeechNl:
          "Ja, ik bevestig de dienst op dinsdag om acht uur en de volgende stap voor de overdracht.",
        recommendedSpeechEn:
          "Yes, I confirm the shift on Tuesday at eight o'clock and the next step for handover.",
        streamingChunks: [
          "Ja, ik bevestig de dienst op dinsdag...",
          "Ja, ik bevestig de dienst op dinsdag om acht uur...",
          "Ja, ik bevestig de dienst op dinsdag om acht uur en de volgende stap voor de overdracht.",
        ],
        ambiguousExampleNl: primaryBreakdown.self_evidence_excerpt,
        repairGuidanceEn:
          "Notice the pacing breakdown from your prior attempt: 'Ja, dank u.' sounds polite, but omits the day/time and next step. Restate 'ik bevestig dinsdag om acht uur' explicitly.",
      },
    ],
  },
  {
    situation: "repair_appointment",
    title: "Apartment Repair Appointment",
    dutchLabel: "Reparatieafspraak · Woningbeheer",
    calendarOpaqueRef: "calref_5a19b044d72e31c8f0217b90",
    calendarProvider: "learner_marked",
    eventWindowLocal: "Today · 15:00–15:20 CEST",
    rehearsalWindowLocal: "14:00 CEST (from real-world-attempt.example.json)",
    timeZone: canonicalAttempt.next_occurrence.time_zone,
    estimatedSeconds: 90,
    targetSkill: "confirmation",
    requiredCriteria: ["goal_stated", "appointment_confirmed"],
    priorAttemptSummary: {
      attemptId: canonicalAttempt.attempt_id,
      beeReference: canonicalAttempt.source.bee_recording_reference,
      priorStatus: canonicalAttempt.outcome.status,
      breakdownCode: primaryBreakdown.code,
      breakdownDescription: primaryBreakdown.description,
      priorSelfExcerpt: primaryBreakdown.self_evidence_excerpt,
      confidence: primaryBreakdown.confidence,
    },
    turns: [
      {
        turnId: "t1",
        counterpartPersona: "property_scheduler",
        pollyVoiceLabel: "Amazon Polly · Ruben (nl-NL · fixture preview)",
        counterpartTextNl: "Vertel alstublieft wat u nodig heeft.",
        counterpartTextEn: "Please explain what you need.",
        expectedFunction: "goal_stated",
        signedAudioUrl:
          "https://audio.nexttime.local/v1/counterpart-clips/repair-t1?expires=900s&mode=fixture",
        supportPhraseId: "goal_repair_basic",
        supportPhraseNl: "Ik wil een afspraak voor de reparatie.",
        supportPhraseEn: "I would like to make a repair appointment.",
        recommendedSpeechNl:
          canonicalAttempt.outcome.criteria[0]?.self_evidence_excerpt ||
          "Ik wil een afspraak voor de reparatie.",
        recommendedSpeechEn: "I would like an appointment for the repair.",
        streamingChunks: [
          "Ik wil een afspraak...",
          "Ik wil een afspraak voor de reparatie.",
        ],
        ambiguousExampleNl: "Hallo, goedemiddag.",
        repairGuidanceEn:
          "Name the repair appointment goal explicitly ('een afspraak voor de reparatie').",
      },
      {
        turnId: "t2",
        counterpartPersona: "property_scheduler",
        pollyVoiceLabel: "Amazon Polly · Ruben (nl-NL · fixture preview)",
        counterpartTextNl:
          "De reparatiewerkplek is vrijdag om tien uur. Kunt u dat bevestigen?",
        counterpartTextEn:
          "The repair slot is Friday at ten o'clock. Can you confirm that?",
        expectedFunction: "appointment_confirmed",
        signedAudioUrl:
          "https://audio.nexttime.local/v1/counterpart-clips/repair-t2?expires=900s&mode=fixture",
        supportPhraseId: "confirm_time",
        supportPhraseNl: "Ja, ik bevestig de datum en de tijd.",
        supportPhraseEn: "Yes, I confirm the date and time.",
        recommendedSpeechNl:
          "Ja, ik bevestig de afspraak op vrijdag om tien uur.",
        recommendedSpeechEn:
          "Yes, I confirm the appointment on Friday at ten o'clock.",
        streamingChunks: [
          "Ja, ik bevestig de afspraak...",
          "Ja, ik bevestig de afspraak op vrijdag om tien uur.",
        ],
        ambiguousExampleNl: primaryBreakdown.self_evidence_excerpt,
        repairGuidanceEn:
          "In examples/real-world-attempt.example.json, 'Ja, dank u.' left appointment_confirmed unknown. Confirm the day and time explicitly.",
      },
    ],
  },
  {
    situation: "school_admin_appointment",
    title: "School Administration Check-In",
    dutchLabel: "Schooladministratie · Afspraak bevestigen",
    calendarOpaqueRef: "calref_3d92a170c41e88b50129fa10",
    calendarProvider: "icalendar",
    eventWindowLocal: "Tomorrow · 10:30–10:45 CEST",
    rehearsalWindowLocal: "10:00 CEST (30 min before appointment)",
    timeZone: canonicalAttempt.next_occurrence.time_zone,
    estimatedSeconds: 90,
    targetSkill: "confirmation",
    requiredCriteria: ["goal_stated", "appointment_confirmed"],
    priorAttemptSummary: {
      attemptId: canonicalAttempt.attempt_id,
      beeReference: canonicalAttempt.source.bee_recording_reference,
      priorStatus: "partial",
      breakdownCode: "confirmation_missing",
      breakdownDescription:
        "Stated the request to meet the school office, but did not confirm Monday at 09:00 before hanging up.",
      priorSelfExcerpt: "Ja, dank u.",
      confidence: 0.82,
    },
    turns: [
      {
        turnId: "t1",
        counterpartPersona: "school_scheduler",
        pollyVoiceLabel: "Amazon Polly · Lotte (nl-NL · fixture preview)",
        counterpartTextNl: "Goedemorgen, waarmee kan de schooladministratie u helpen?",
        counterpartTextEn: "Good morning, how can the school office help you?",
        expectedFunction: "goal_stated",
        signedAudioUrl:
          "https://audio.nexttime.local/v1/counterpart-clips/school-t1?expires=900s&mode=fixture",
        supportPhraseId: "goal_school_basic",
        supportPhraseNl: "Ik wil een afspraak met de schooladministratie.",
        supportPhraseEn: "I would like an appointment with the school office.",
        recommendedSpeechNl:
          "Goedemorgen, ik wil graag een afspraak met de schooladministratie maken.",
        recommendedSpeechEn:
          "Good morning, I would like to schedule an appointment with the school office.",
        streamingChunks: [
          "Goedemorgen, ik wil graag...",
          "Goedemorgen, ik wil graag een afspraak met de schooladministratie maken.",
        ],
        ambiguousExampleNl: "Goedemorgen.",
        repairGuidanceEn:
          "State that you are calling for an appointment with the school administration.",
      },
      {
        turnId: "t2",
        counterpartPersona: "school_scheduler",
        pollyVoiceLabel: "Amazon Polly · Lotte (nl-NL · fixture preview)",
        counterpartTextNl:
          "De schooladministratie kan maandag om negen uur. Kunt u dat bevestigen?",
        counterpartTextEn:
          "The school office has Monday at nine o'clock available. Can you confirm that?",
        expectedFunction: "appointment_confirmed",
        signedAudioUrl:
          "https://audio.nexttime.local/v1/counterpart-clips/school-t2?expires=900s&mode=fixture",
        supportPhraseId: "confirm_time",
        supportPhraseNl: "Ja, ik bevestig maandag om negen uur.",
        supportPhraseEn: "Yes, I confirm Monday at nine o'clock.",
        recommendedSpeechNl:
          "Ja, ik bevestig de afspraak op maandag om negen uur.",
        recommendedSpeechEn:
          "Yes, I confirm the appointment on Monday at nine o'clock.",
        streamingChunks: [
          "Ja, ik bevestig de afspraak...",
          "Ja, ik bevestig de afspraak op maandag om negen uur.",
        ],
        ambiguousExampleNl: "Ja, dank u.",
        repairGuidanceEn:
          "Restate 'maandag om negen uur' or 'ik bevestig' so both sides agree on the slot.",
      },
    ],
  },
];

export function redactDeterministicPii(raw: string): { text: string; redacted: boolean } {
  const emailPattern = /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g;
  const phonePattern = /(?:\+\d{1,3}[\s-]?)?(?:\(?\d{2,4}\)?[\s-]?)?\d{6,10}\b/g;
  let cleaned = raw.trim();
  const before = cleaned;
  cleaned = cleaned.replace(emailPattern, "[redacted]");
  cleaned = cleaned.replace(phonePattern, "[redacted]");
  return { text: cleaned, redacted: cleaned !== before };
}

export function evaluateDutchTurnLocally(
  turn: ScenarioTurnSpec,
  transcript: string,
  supportPhraseId?: string
): { mappedFunction: string | null; satisfied: boolean } {
  if (supportPhraseId && supportPhraseId === turn.supportPhraseId) {
    return { mappedFunction: turn.expectedFunction, satisfied: true };
  }
  const lower = transcript.toLowerCase();
  if (turn.expectedFunction === "goal_stated") {
    const hasGoal =
      lower.includes("afspraak") ||
      lower.includes("dienst") ||
      lower.includes("standup") ||
      lower.includes("reparatie") ||
      lower.includes("schooladministratie") ||
      lower.includes("volgende stap") ||
      lower.includes("controleren") ||
      lower.includes("afstemmen");
    return {
      mappedFunction: hasGoal ? "goal_stated" : null,
      satisfied: hasGoal,
    };
  }
  const hasConfirmation =
    lower.includes("bevestig") ||
    lower.includes("akkoord") ||
    lower.includes("vrijdag") ||
    lower.includes("dinsdag") ||
    lower.includes("maandag") ||
    lower.includes("acht uur") ||
    lower.includes("tien uur") ||
    lower.includes("negen uur") ||
    lower.includes("overdracht") ||
    lower.includes("datum en de tijd") ||
    lower.includes("volgende stap");
  return {
    mappedFunction: hasConfirmation ? turn.expectedFunction : null,
    satisfied: hasConfirmation,
  };
}

export function buildHapticRecord(
  trigger: string,
  hapticsEnabled: boolean
): HapticEventRecord {
  const isSteady =
    trigger === "ptt_recording_started" || trigger === "turn_budget_boundary";
  const pattern: HapticPattern = hapticsEnabled
    ? isSteady
      ? "STEADY_BUZZ"
      : "SHORT_DOUBLE"
    : null;
  const timingLabel =
    pattern === "STEADY_BUZZ"
      ? "180ms single pulse"
      : pattern === "SHORT_DOUBLE"
        ? "60ms · 80ms gap · 60ms (200ms)"
        : "Suppressed (visual caption + VoiceOver active)";

  const announcements: Record<string, [string, string]> = {
    counterpart_turn_ready: [
      "rehearsal.caption.counterpart_ready",
      "Counterpart prompt ready. Press and hold or use non-speaking input when ready.",
    ],
    ptt_recording_started: [
      "rehearsal.caption.recording_active",
      "Recording started. Speak your Dutch turn; haptics pause until release.",
    ],
    ptt_recording_released: [
      "rehearsal.caption.processing_speech",
      "Speech captured and raw PCM deleted. Processing minimized Dutch turn.",
    ],
    support_repair_offered: [
      "rehearsal.caption.repair_support",
      "Try restating the concrete time or next step to complete confirmation.",
    ],
    turn_budget_boundary: [
      "rehearsal.caption.final_turn_boundary",
      "Turn 1 complete. Advancing to final turn of your 90-second rehearsal.",
    ],
    rehearsal_completed: [
      "rehearsal.caption.practice_completed",
      "Practice completed. Reviewing post-rehearsal pacing and functional breakdown.",
    ],
  };
  const pair = announcements[trigger] ?? [
    "rehearsal.caption.turn_boundary",
    "Rehearsal turn boundary.",
  ];

  return {
    eventId: `hap_${Math.random().toString(36).slice(2, 10)}`,
    trigger,
    pattern,
    timingLabel,
    emitted: hapticsEnabled,
    suppressionReason: hapticsEnabled ? null : "learner_haptics_disabled",
    visualCaptionKey: pair[0],
    voiceoverAnnouncement: pair[1],
    targetDevice: "cli_rehearsal_simulator",
  };
}
