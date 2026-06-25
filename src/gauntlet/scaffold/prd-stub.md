<!-- GAUNTLET-PRD-STUB: replace this file with a real PRD -->
<!-- gauntlet-generated: true; gauntlet-template-version: 1 -->
<!--
  This is a STRUCTURED STUB, not a PRD. Gauntlet does not author PRDs (FR-10.1):
  a human fills this in, then runs `gauntlet run <slug>`. The run gate refuses to
  start while the marker above is present or the body is still this skeleton.
  For the full authoring playbook (interview process, the bar a draft must clear),
  open `prompts/prd-author.md` (resolved under this repo's asset_root). Each
  section below carries a one-line guidance comment; replace it with real content.
-->

# PRD: <Working name>

<!-- Header block (mandatory): fill in the metadata, one value per label. -->
**Status:** Draft v0.1
**Author:** <you>
**Date:** <YYYY-MM-DD, absolute>
**Working name:** <short name>
**Relationship to existing artifacts:** <does this amend any approved artifact? almost always no; name the machinery it builds on>

## §1 Overview

<!-- Mandatory. The spine: problem, solution, and the riskiest belief. -->

### 1.1 Problem statement
<!-- What hurts today, for whom, why it matters — concrete, not aspirational. -->

### 1.2 Solution summary
<!-- What you will build, in one or two paragraphs; the shape of the approach. -->

### 1.3 The assumption this validates
<!-- The riskiest belief the feature rests on, stated so a phase can prove it. -->

## §2 Goals and Non-Goals

<!-- Mandatory (Non-Goals especially — your sharpest weapon against scope creep). -->

### 2.1 Goals
<!-- A table of G1, G2, … each a single outcome mapped to the need it serves. -->

### 2.2 Non-Goals (v1)
<!-- Explicit, bulleted, slightly painful to write — every "not X (yet)" you record. -->

## §3 Users and Personas

<!-- Scale-with-size. Who touches this and what they do with it. -->

## §4 System Architecture

<!-- Scale-with-size (usually present). -->

### 4.1 Components
<!-- Name the real modules/files this adds or touches; new vs. reused. -->

### 4.2 Key design decisions
<!-- A table of decision / choice / rationale; answer "why not the obvious alternative?". -->

## §5 Functional Requirements

<!-- Mandatory (the core). Number FR-1, FR-2, …; each ENDS with a testable `Acceptance:` line. -->

## §6 Data & Schemas (normative excerpts)

<!-- Present whenever there is structured I/O: the literal shape of any artifact. -->

## §7 Security & Privacy

<!-- Present whenever the feature touches execution, secrets, network, or the judge: state the fail-closed default. -->

## §8 Implementation Plan (phased, assumption-validating)

<!-- Mandatory. A table of phases P1, P2, … each with its deliverable and the assumption it validates, ordered riskiest-first; no forward dependencies. -->

## §9 Success Metrics

<!-- Mandatory. Measurable outcomes — numbers and thresholds, not adjectives. -->

## §10 Risks & Mitigations

<!-- Scale-with-size. A table pairing each risk with a concrete mitigation. -->

## §11 Open Questions

<!-- Mandatory if any exist (and some always do). State each plainly; mark resolutions inline. -->
