# Cover engine C4: cover line eval

2026-11-23, staging. Prompt P12 on the 50 newest clips that have a transcript and a title (`sixstars covers eval-lines --limit 50`).

- **Clips with at least one valid line:** 50 of 50. The bar is 45.
- **Invented content:** none kept. Every kept line's `source` is in the transcript (fuzzy ≥ 0.8); the 8 candidates whose words were not are dropped.
- **Model:** claude-haiku-5-5.
- **Cost:** $0.0084 for the run, **$0.0168 per 100 clips**.
- **Dropped candidates by reason:** repeats_title 1, source_not_in_transcript 8, spoils_outcome 2, too_long 7, words=6 7, words=9 1.
- **To watch:** "I THINK I'M BLACK" (said about an in-game skin) passes the text rules. Out of context it can read badly, so the C6 vision check's `safe` score must judge it. Check it in the C6 staging run.

| Title | Kept lines | Dropped (reason) |
|---|---|---|
| Jerma's drive takes a very chaotic turn | NO! NO! NO! (quote); SHOOT OUT THE WINDOW (quote); VIOLENCE ALLOWED? (label) | — |
| Cohh's stealth run has one big problem | THIS IS TORTURE (quote); WITHOUT A CONTROLLER (quote); WRONG WEAPON, WRONG GAME (label) | — |
| MissMikkaa checks on Jason and chat loses it | HE LANDED THE PLANE? (quote) | BOYFRIEND DIED (spoils_outcome); PLANE FALL FAIL (source_not_in_transcript) |
| MissMikkaa's chat loses it over this moment | LITTLE CAT FIGHT (quote); JESUS CHRIST (quote); STREET BRAWL REACTION (label) | — |
| Jerma's survival run takes a very sudden turn | HE BLEW IT UP (quote); REMOTE DETONATION (label); OH SH*T (quote) | — |
| ennaouii just wants a fair fight and the game says no | NO FIGHTING, OKAY? (quote); THIS IS THE REAL PART (quote); STREAMER STARTS A FIGHT (label) | — |
| ennaouii steals a scooter and the cops are on him | I'M ABOUT TO GET SHOT (quote); I THINK I'M BLACK (quote) | STOLEN SCOOTER FAIL (source_not_in_transcript) |
| ennaouii's bird grab has the whole street calling the cops | GET A WANTED LEVEL? (quote); F*CK YOU, SERIOUSLY (quote); BIRD GRAB GONE WRONG (label) | — |
| ennaouii tries to ride a motorbike with the big gun | SCARE ME TO DEATH (quote); F*CK, COME ON (quote); SHOOTING WHILE MOVING (label) | — |
| ennaouii picks up the weirdest travel buddy in Leonida | TAKING HIM HOME (quote); NOBODY'S STOPPING ME (quote); BIRD ON A BIKE (label) | — |
| ennaouii's brand new car has a very short life | JUST GET ANOTHER ONE (quote); AH, F*CK. IT'S FINE (quote); CAR WRECKED FAST (label) | — |
| montanablack88 finds a wild Easter egg and chat starts clapping | HOLY SH*T (quote); THAT WAS SO GOOD (quote) | WILD GTA 6 EASTER EGG (repeats_title) |
| MontanaBlack finds a very familiar face in the game | HOLY SH*T (quote); DRAGON LORD? (quote); LOOKALIKE NPC (label) | — |
| montanablack88 finds a wild Easter egg and chat loses it | WHAT IS THIS (quote); WTF IS THIS (quote); HIDDEN EASTER EGG (label) | — |
| quin69 almost gets blown up and chat can't handle it | THAT WILL KILL (quote); NEAR-MISS EXPLOSION FAIL (label) | I THOUGHT I'D GO THROUGH THAT (words=6, too_long) |
| quin69's heist crew is falling apart | STARTED SHOOTING ME (quote); TEAMMATE TURNS ON HIM (label) | HE JUST STARTED SHOOTING ME (too_long) |
| quin69 gets absolutely humbled in the middle of a shootout | CHICKENS F*CK (quote); NO WAY! (quote); CHAT'S CHICKEN QUEST (label) | — |
| quin69 is NOT having a chill stream right now | IT'S SLOW (quote); I THOUGHT IT WAS GOOD (quote); A RUNNING FAIL (label) | — |
| Quin pays an NPC for the worst life advice | QUIT YOUR JOB (quote); YOU'LL REGRET IT (quote); PAYING FOR SH*T TAKES (label) | — |
| quin69 walks onto a cartel rooftop totally unprepared | COME FU*K YOU UP (quote); ROOFTOP AMBUSH FAIL (label) | THEY'RE WAITING ON A GREEN LIGHT (words=6, too_long) |
| rdcgaming gets a surprise message from a voice actor in chat | THANK YOU FOR PLAYING (quote); CHIMP MISSION YET? (quote); CAST THANKS STREAMER (label) | — |
| rdcgaming's aim betrays him when it matters most | WHY DOES IT DO THAT? (quote); THEY HOLD ME! (quote); SHOTS KEEP MISSING (label) | — |
| rdcgaming's carjack attempt goes sideways fast | JUST GO GO GO (quote); YOU'RE F*CKING STUPID (quote); CARJACK GOES WRONG (label) | — |
| rdcgaming checks his criminal profile and chat can't stop laughing | CHECKING HIS PROFILE (label); HOW DO I LOWER IT (quote); I AIN'T BEEN VIOLENT (quote) | — |
| skywhywalker's mom steps in and the chat goes wild | MOM TAKES THE SHOT (label); WELL DONE, MOM (quote); GREAT MOVE, MOM (quote) | — |
| skywhywalker picks the worst outfit for a visit to mom | SWIMSUIT TO MOM? (quote); PERFECT SET FOR MOM (quote); WRONG OUTFIT PICK (label) | — |
| adm1tr1pow's big purchase is not what he expected | THIS IS A DISASTER (quote); F*CK, DEPOSIT DOWN (quote); MESSY OFFICE BUY (label) | — |
| Rubius picks the worst possible moment to call Lucy | BAD TIMING (label); F*CK, LUCY! (quote); I DIDN'T SEE THE CAR (quote) | — |
| domingo reacts to something wild made with AI | THE BREAK IS SOMETHING ELSE (quote); AI VIDEO MADE HIM SPEECHLESS (label) | I WAS LIKE THAT'S SOMETHING ELSE (words=6, too_long) |
| Gotaga hits the wrong button at the worst time | WRONG DANCE BUTTON (quote); THAT'S NOT WHAT I MEANT (quote); MISCLICK AT THE WORST TIME (label) | — |
| tusoved can't figure out why the cops won't shoot | THE MORE STARS, RIGHT? (quote); GET IN HERE! (quote) | GRENADES, MORE STARS (source_not_in_transcript) |
| lru_rose finds something very wrong in this hideout | SH*T IN THE SINK? (quote); PUT HER POT IN THERE (quote); SINK DISASTER (label) | — |
| Rostislav's rooftop jump plan has ONE rule | ONTO A MOVING CAR (quote) | IT WAS ONTO A MOVING CAR (words=6); ROOFTOP JUMP FAIL (source_not_in_transcript) |
| royalistiq's save situation gets scary real quick | WE HAVEN'T SAVED (quote); I CAN'T SAVE (quote); NO SAVE POINT (label) | — |
| royalistiq can't believe what he's seeing | IT'S A F*CKING CINEMA (quote); NO WAY I'M FLYING (quote); PLANE VIEW SHOCK (label) | — |
| royalistiq gives an NPC a very aggressive tennis lesson | ANOTHER BACKHAND? (quote); HOW DOES THE GRASS TASTE? (quote) | NPC GETS SLAPPED FOR TALKING BACK (words=6, too_long) |
| royalistiq runs into a face he did NOT expect | THIS IS JIMMY DE SANTA (quote); A GTA EASTER EGG FOUND (label) | I JUST BLEW UP FROM SUCCESS (words=6) |
| royalistiq's getaway takes a turn nobody saw coming | FORGOT THERE WAS WATER HERE (quote); IT'S NOT BROKEN (quote); VEHICLE LANDS IN WATER (label) | — |
| recrent's driving gets way too fast and chat can't stop laughing | WHAT A CARD CAR (quote) | HOW IS THE ENGINE SO EASY TO MESS UP (words=9, too_long); CAR CRASH FAIL (source_not_in_transcript) |
| recrent's helicopter piloting has chat in tears | IT'S SCRIPTED (quote); F*CK, A SECOND BEFORE (quote); HELI CRASH FAIL (label) | — |
| recrent's driving gets a little too confident | DIDN'T SEE IT (quote); MISSED THE TURN (label) | PINK ARROW CRASHED ME (spoils_outcome) |
| recrent's ladder climb goes totally sideways | I SAID ALL CLEAR (quote); WHAT DO YOU MEAN (quote) | LADDER GETS SHOT (source_not_in_transcript) |
| recrent's bike confidence gets tested | FLEW OFF (quote); OH, F*CK, HOW SAD (quote); A COUPLE FLEW OFF (label) | — |
| dmitry_bale's weirdest ride enters a race | HOW IS HE WINNING (quote); THIS IS A LEGEND (quote); WRONG WAY RACE (label) | — |
| dmitry_bale watches an NPC's car moment go totally sideways | NOT TOUCHING YOU TODAY (quote); NPC CAR FALL GOES WRONG (label) | THIS CAR, MY GUY BOUGHT IT (words=6) |
| dmitry_bale's parachute jump off the tower goes sideways | SCREW THEM! (quote); ARE YOU F*CK SERIOUS? (quote); WALKS UP TO ALIENS (label) | — |
| dmitry_bale's furry rescue mission takes a wild turn | YOU KILLED HIM! (quote) | THEY'RE OPPRESSING FURRIES (too_long); FURRY RESCUE GONE WRONG (source_not_in_transcript) |
| lru_rose's helpful NPC situation gets out of hand | HELPFUL NPC (quote); WHY THE F*CK (quote) | NPC TOOK MY CAR (source_not_in_transcript) |
| gagik's jaw drops when this face shows up | NIKO BELLIC?! (quote); F*CK, IT'S NIKO (quote); GTA 4 HERO RETURNS (label) | — |
| tusoved's stream takes a very weird turn | WHAT DID I GET INTO (quote); UNDRESS? NO WAY (quote); A WEIRD DETOUR (label) | — |
