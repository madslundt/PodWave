# PodWave listening evaluation

[`quality-briefs.json`](quality-briefs.json) fixes six inputs for before/after comparison:
fiction, nonfiction and scheduled digests in English and Danish, with solo and two-host formats.
The fictional headlines are example data. Local mock tests check that these inputs reach the
writer; they do not measure script or voice quality. No live results are recorded here, and
the script prompts are unchanged pending evidence of a specific quality gap.

## Before/after listening

1. Use isolated test data and the same model, voices, input, and source snapshot for the baseline
   and candidate. Record the revisions and non-secret settings. For manual cases, queue the
   `brief` in Create. For digest cases, run one feed-only show with the fixture settings and use
   a controlled RSS feed with the two headlines and current timestamps. Pin the same cast via
   `auto.shows[].hosts`; disable external search for the fictional headlines. Keep the two variants'
   histories separate so previous picks cannot affect the comparison.
2. Save the scripts, downloadable WAVs, source excerpts/URLs and runtimes. Shuffle the sample
   labels. Two reviewers who understand each sample's language score it independently before
   discussing. Record timestamps, excerpts or source URLs for every miss.
3. For each variant, report the median of reviewer scores on each dimension across the six
   samples and show individual scores. The target is at least 4/5 on **each** dimension, no
   fabricated citations in the reviewed nonfiction set, and no unresolved main arc in fiction.

| Dimension | 1 | 3 | 5 |
|---|---|---|---|
| Opening | No hook or promise | Topic clear; hook arrives late | Immediate hook sets up the question the episode answers |
| Clarity | Hard to tell who, what or why | Mostly clear, with some jumps | Easy to follow without prior knowledge |
| Pacing | Filler, rushed essentials or repeated blocks | Mostly balanced, with a slow or abrupt section | Every section earns its time and leads naturally to the next |
| Novelty | Mostly recap or a list | Some new ideas, but familiar points recur | Each section adds a distinct fact, decision or consequence |
| Payoff | Opening question or conflict left open | Partial answer or rushed ending | Resolves the main arc or answers the opening question without a recap dump |

Use 2 and 4 for scores between the anchors. Separately record pass/fail and evidence for:

- **Nonfiction:** Trace material factual claims to actual sources. Flag unsourced, inaccurate,
  or invented claims and citations. Feed headlines alone do not support added facts.
- **Fiction:** Track character details and motivations; check setup, consistency and resolution.
- **Audio:** In both languages and both formats, listen for clean joins, intelligibility, even
  speaker levels, unwanted silence and missing or cut-off speech. Check Library playback and
  seeking, plus offline playback of the downloaded WAV. Listen at 1x and compare the runtime
  to the brief using the greater of 20% or one minute as tolerance.
- **Series:** Listen in Library order to a three-part manual series and two drops of one show.
  Include an exact repeat, an update and another topic in the second feed snapshot. Check host
  continuity, whether each episode stands alone, and what new information it adds. The scheduler
  tracks previously used items by URL, not semantic similarity between different URLs.

| Fixture / variant | Reviewer | 5 scores | Claim or arc check | Audio check | Runtime | Sample and evidence |
|---|---|---|---|---|---|---|
| | | | | | | |
