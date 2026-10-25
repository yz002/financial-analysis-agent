# Extension QA checklist

The manual end-to-end check for the Chrome extension and the backend it talks to. It was first
run in Phase D session 7 (2026-10-25); the results are in `docs/chrome-extension-design.md` §9
item 7.

## How to use it

- **Run it before a release, and again after a fix round.** Steps marked **[P]** use Render
  production with the production build. Steps marked **[L]** use the local backend, which
  writes to the **shared production database** (`backend/DEPLOYMENT.md`, "Known risk").
- **Record every step** as pass, fail, not met or skipped, with a short note for anything other
  than a pass. Put the results in the design doc's §9 entry for that session, not in this file.
- **Quotes and `file:line` references are as of commit `46fe4a5`.** Before the next run,
  refresh any quote whose code has changed from the code itself, never from memory, and keep
  the reference beside it.
- **BYO key (steps 51–56, 68):** create a **new, dedicated** Anthropic test key for the run.
  Paste it **only into the panel's key field**: never into chat, an assistant session or any
  file. **Delete it in the Anthropic console afterwards** (step 68).
- **Never paste secrets into notes or results:** Stripe keys, webhook secrets, Anthropic keys,
  `BYO_KEY_ENCRYPTION_KEY`. Step 65 searches the Render logs for their prefixes; record only
  whether there were matches.

## Steps

**[L]** means local backend (shared prod DB), **[P]** means Render production with the production
build. Before any [L] step, check for exactly one listener on 8000:
`Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object LocalAddress, OwningProcess`
(backend/DEPLOYMENT.md, "Local development").

Abbreviations: `main` = `extension/entrypoints/sidepanel/main.ts`; `usage` / `chat` = the same
folder; `um` = `extension/lib/usageModel.ts`; `cm` = `lib/chatModel.ts`; `mm` = `lib/mappingModel.ts`;
`sl` = `lib/statementLabel.ts`; `cd` = `lib/connectData.ts`; `ar` = `lib/askRunner.ts`;
`html` = `sidepanel/index.html`. `${…}` is a template slot.

**Setup and install**
1. [L] Run `cd extension; npm run compile; npm test`. Expected: compile clean, all tests pass.
2. [L] Start the local backend from `backend/` with
   `.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000` (no `--reload`), then
   run `npm run build:local` in `extension/` and load or reload `extension\.output\chrome-mv3`.
   Expected: the panel shows `Backend: http://127.0.0.1:8000` (main:93; the value comes from
   `extension/.env.development`).
3. [P] Run `npm run build` in `extension/` and reload the same folder. Expected: no "Backend:"
   line (the block at main:90 is skipped in production).
4. [P] Open the side panel on a Sheet tab. Expected: `Sign in to connect your spreadsheet.`
   (html:17).

**Sign-in**
5. [P] Sign in with Google. Expected: `Signed in via Google · account ${accountId}` (main:651).
6. [P] Sign out, then sign in with Microsoft. Expected: `Signed in via Microsoft · account …`
   (main:651).

**Google Sheets**
7. [P] On the test sheet, click Connect data. Expected:
   - `Connected: ${file.name} via ${token.email}` (main:931);
   - below Preview, `Press Enter in the sheet before Preview: a cell still being edited isn't
     read.` (html:73–74; shown for Google only, main:351).
8. [P] Expected under Sheet: `Added a tab? Click Connect data again to see it.` (html:60).
9. [P] Add a tab in the sheet, then click Connect data. Expected: the new tab is listed.
10. [P] Type in a cell without pressing Enter, then Preview. Expected: the old value is in the
    preview.
11. [P] Press Enter, then Preview. Expected: the new value.
12. [P] Preview `A3:G7` on 'P&L'. Expected: the caption reads `'P&L'!A3:G7 · ${n} data rows ×
    ${m} columns` (main:404), and Send to analysis is visible.
13. [P] Change Sheet. Expected: the preview, caption and range are cleared, and Send is hidden
    (main:1277).
14. [P] Preview a range with more than 10 data rows. Expected: the caption ends ` · showing the
    first 10` (main:405), and the toggle reads `Show all ${dataRows} rows` (main:411).
15. [P] Click it. Expected: all rows show with no new read, the label is `Show first 10`
    (main:411), and the caption loses "showing the first 10".
16. [P] Preview 'P&L'!A3:G7, then Send to analysis. Expected: `Suggesting a mapping…`
    (main:1034), then a role per column, and the button `Reset to suggestion` (main:288).
17. [P] Check Business name. Expected: no trailing `.xlsx/.xlsm/.xlsb/.xls/.csv` (mm:75). A
    Google Sheet name is unchanged.
18. [P] Expected:
    - Confirm is disabled, and the scale menu reads `Choose…` (main:563);
    - the error `Choose the units the numbers are in.` (mm:180);
    - under the menu, `Choose Ones (as shown) unless the sheet says its numbers are in thousands,
      millions or billions.` (html:95–97).
19. [P] Choose `Ones (as shown)` (mm:55), then Confirm mapping. Expected:
    - `Statement confirmed.` (main:1215);
    - the card has `numbers in ones (as shown)` (main:617);
    - the count reads `${periodCount} ${cadence} periods`, e.g. "4 quarterly periods" (sl:28–33).
20. [P] Expected: one line, `Not mapped: ${labels}.` (mm:216, `notMappedLine`), using the labels at mm:25–37, with
    no snake_case. Other backend warnings still show as worded.
21. [P] Ask "What was revenue in the latest quarter?" Expected:
    - above the chat, `Asking about ${describeStatement}.` and `Answers use this statement,
      whichever tab is open.` (chat:280–281);
    - a source list under `Where the figures come from` (chat:258);
    - when all figures are traced, the banner `Every figure in this answer was found in your data
      or the sources used.` (cm:271).
22. [P] Click Change mapping. Expected: the reset button reads `Reset to confirmed mapping`
    (main:289; shown at main:520).
23. [P] Change one role, then click it. Expected: the confirmed roles are restored, with no
    network call.
24. [P] Choose `Thousands` (mm:56) and confirm. Expected: the card has `numbers in thousands`
    (main:617), and the chat transcript is empty (a new conversation).
25. [P] Ask the same question. Expected: the figure is 1,000× the ones answer, with the same cell
    cited.

**Excel Online**
26. [P] Excel file (`onedrive.live.com` shape), then Connect data. Expected:
    - `Data as of last save: ${defaultFormatTime(file.modifiedAt)}. Edits from the last minute
      or two may not appear yet.` (main:345–346), with the time to the minute and no seconds
      (um:19–20);
    - the Enter hint is hidden (main:351).
27. [P] The `excel.cloud.microsoft/open/onedrive/` shape. Expected: `Connected: …` (main:931).
28. [P] (Optional) An `officeapps.live.com` URL. Expected: `Connected: …` (main:931), or `This
    Excel link type isn't supported yet. Open the file from onedrive.live.com or SharePoint, then
    click Connect data again.` (cd:25–26). Record which.
29. [P] Preview the P&L range, Send, choose Ones, and confirm. Expected: as in steps 19–20.
30. [P] Ask a margin question. Expected: a derived figure listed with its formula and cells under
    `Where the figures come from` (chat:258). The formula is in plain words, e.g. `Gross margin,
    period ending ${period_end} = Gross profit / Revenue`; a growth rate reads `(Revenue -
    Prior-period revenue) / Prior-period revenue` (cm `formulaLabel`, cm:166–185). No snake_case.

**Cards, chat and cross-window**
31. [P] Close and reopen the panel. Expected: the card is restored with `Read another range`
    (html:126), and the same `Not mapped: ${labels}.` line as at confirm (mm:216), restored from
    the statement's stored `conceptsUnavailable` (main:620–621). Other warnings and `No value
    for:` are not restored (confirm-time only). A card shown from another window's confirm has
    the line too.
32. [P] With no file connected, click it. Expected: `Connecting…` (main:902), then `Connected: …`
    (main:931). It's a Connect data click (main:1251).
33. [P] After a confirm in this panel, click it. Expected: the read panel and last preview come
    back with no network call, and the card stays.
34. [P] Panels in two windows: ask a question in window 2, then confirm a statement in window 1.
    Expected: window 2's transcript clears with `A new conversation was started in another
    window.` (chat:278). It clears on window 2's next question. In window 1, no notice. (Failed in session 7 and fixed in the fix round, F1.)
35. [P] Ask a question whose answer repeats a figure. Expected:
    - every mention is marked in the text;
    - the source list has one entry for it with `×${mentions} mentions` (chat:109);
    - the same figure from different cells stays as separate entries.
36. [P] Open another spreadsheet tab without confirming. Expected: the boxed line still reads
    `Asking about …` / `Answers use this statement, whichever tab is open.` (chat:280–281),
    naming the confirmed statement.
37. [P] Ask "Summarize revenue and margins as a bullet list with a bold total, then as a table."
    Expected:
    - bullets, bold and the table render as elements, with no raw `**` or `|---|`
      (`answerText`, chat:217);
    - every figure keeps its mark and marker, and the sources list matches;
    - a link shows as plain text;
    - a figure's marker (`Found in more than one place` / `Not traced to your data`) is in the
      same warning color as its status in the sources list (`.figure-marker,
      .chat-source-status`, style.css), in light and dark.
38. [P] Dark mode, mapping screen, Confirm disabled. Expected: a neutral grey fill, grey text and
    an outline (`--color-disabled-*`, tokens.css), not faded mint. Check the same in light mode.

**Free limit**
39. [P] Free account. Expected:
    - on the first load of the panel, the card shows `Loading your usage…` (usage:77, shown at
      usage:175) until GET /v1/usage answers; a later Refresh keeps the last values instead;
    - then `Free: ${n} of 5 questions today` (um:59), and `Resets daily at 00:00 UTC
      (${formatTime(usage.resets_at)} your time).` (um:62), to the minute (um:19). It's the same
      line at 0 used.
40. [P] Click Refresh. Expected: the button reads `Updating…` (usage:80) and is disabled, then
    reads `Refresh` (html:32) again.
41. [P] Ask until 5 of 5. Expected: the count follows each question (main:183).
42. [P] Ask a 6th. Expected:
    - `You've used today's questions. More are available at 00:00 UTC (${time} your time).`
      (cm:313 and cm:316);
    - the question is put back in the input (chat:600);
    - `Upgrade: $2/month` is visible (html:30).
42a. [P] (Paid, optional) At 50 of 50, ask again. Expected: `You've used all the questions for
    this billing period. More are available from ${time}.` (cm:306–307), with no UTC wording.
42b. [P] (Optional) After the mapping-suggestion cap. Expected: `You've used today's suggested
    mappings (more from ${time}). Choose what each column is yourself.` (main:1092–1095). It's a
    rolling window, so there's no UTC wording.

**Paid via checkout**
43. [P] Click Upgrade. Expected: a `checkout.stripe.com` tab, and `Finish checkout in the new tab.
    This panel updates once Stripe confirms.` (usage:238).
44. [P] Pay with the test card and return. Expected: `Waiting for Stripe to confirm…` (usage:74),
    then `Paid: 0 of 50 questions this period` (um:41) and `Renews ${time}.` (um:48).
45. [P] Ask a question. Expected: `Paid: 1 of 50 questions this period` (um:41).

**Portal cancel**
46. [P] Click Manage subscription. Expected: a `billing.stripe.com` tab, and `Make your changes in
    the new tab. This panel updates once Stripe confirms.` (usage:239).
47. [P] Cancel at period end, return, and keep the panel visible for up to about 3 min. Expected:
    `Your subscription ends ${time}.` (um:44), with no manual Refresh. A late event is caught by 8
    quiet checks 15 s apart (usage:69–70).
48. [P] Open Manage, then return without changes. Expected: `Waiting for Stripe to confirm…`
    (usage:74) clears after about 1 min. The quiet checks continue for about 2 min with no note.
    No error.
49. [P] Renew in the portal and return. Expected: `Renews ${time}.` (um:48).
50. [P] Cancel immediately. Expected: back to the free line, where today's paid questions count
    (EI §6 /v1/usage, by design): `Free: ${n} of 5 questions today` (um:59) at or under the cap,
    or `Free: ${n} questions counted today (daily limit 5)` (um:58) over it, never "6 of 5".

**BYO key**

> **Before step 51:** create a **new, dedicated** Anthropic test key in workspace
> `fa-byo-live-test` (the old one was deleted). Paste it **only into the panel's key field**:
> never into chat, Claude Code or any file. **Delete the key in the Anthropic console after QA.**

51. [P] Paste the key and click Save key. Expected: `Checking the key with Anthropic…`
    (usage:321), then `Key saved. Your questions now use your own Anthropic key.` (usage:324), and
    the line `Using your own Anthropic key: questions are billed to your Anthropic account.`
    (um:39).
52. [P] Ask a question. Expected: it's answered, and the usage line stays as in um:39. After
    step 53, the Free/Paid count equals its value before step 51 (BYO questions aren't counted).
53. [P] Click Remove key. Expected: `Key removed. Your questions use your plan again.`
    (usage:347).
54. [L] Save a well-formed fake `sk-ant-…`. Expected: `Anthropic didn't accept that key. Check it
    and try again.` (um:136).
55. [P] Save `hello`. Expected: `That doesn't look like an Anthropic API key. Keys start with
    sk-ant-.` (um:141).
56. [P] Click Save key with an empty field. Expected: `Paste your Anthropic API key first.`
    (usage:315). After every attempt the input is empty (usage:312, usage:335).

**Error states** (reordered: connect and preview while the backend is up)
57. [L] With the local backend running (one listener), connect the test sheet and preview a range.
    Expected: the preview shows and Send to analysis is visible.
58. [L] Stop uvicorn (Ctrl+C), then click Refresh. Expected: `Couldn't load your usage. Select
    Refresh to try again.` (usage:189), and the button reads `Refresh` again.
59. [L] With the backend still stopped, ask a question. Expected: a network failure is retried,
    not failed (ar:66).
    - The waiting text: `Answering… this can take a few minutes. You can close the panel; the
      answer is kept.` (chat:276).
    - Click Stop waiting. Expected: `Stopped waiting. The answer may still arrive: select Check
      again.` (cm:327), and Check again appears (chat:583).
60. [L] With the backend still stopped, click Send to analysis. Expected: `Could not send the data
    — please try again.` (main:1045).
60a. [L] With the backend still stopped, click Connect data on a Sheet. Expected: `Couldn't
    connect. Check your connection and try again.` (main:849). Added with the session 7 fix
    round (F9).
61. [L] Restart uvicorn (check for exactly one listener), then click Check again. Expected: the
    same question is resent with the same `request_id` and answered, counted once. Refresh then
    loads usage.
62. [P] Open the Google Sheets home page (`https://docs.google.com/spreadsheets/`, the file list,
    with no sheet open), open the panel, and click Connect data. Expected: `Open a Google Sheet or
    an Excel file first.` (main:912). The panel is enabled there (background.ts:6–12), but
    `classifyTab` needs `/spreadsheets/d/<id>` (cd, `classifyTab`).
62a. [P] Range `A3:`, then Preview. Expected: `Enter a range like A3:G7, or leave it empty for the
    whole sheet.` (main:957).
63. [P] Set Revenue to Not used. Expected: Confirm is disabled, with `Choose the revenue column.
    Revenue is required.` (mm:166).

**Latency and logs**
64. [P] DevTools, then Network, then Refresh 5×. Record each `/v1/usage` time. Close the item if
    every time is under about 1 s; otherwise record the times (see `docs/chrome-extension-design.md` §10 item 15).
65. [P] Search the Render logs for `sk-ant`, `whsec` and `sk_test`. Expected: no matches.

**Sign-out**
66. [P] Click Sign out. Expected: the status `Signing out…` (main:769), then `Sign in to connect
    your spreadsheet.` (html:17).
67. [P] Sign in, then click Sign out everywhere. Expected: `Signing out everywhere…` (main:793),
    then the signed-out view in every window.
68. [P] Delete the step 51 key in the Anthropic console.
