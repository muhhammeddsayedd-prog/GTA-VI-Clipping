# P10: sentence_ends

## System

{preamble}

Your job here: a speech-to-text model transcribed about a minute of a stream and left out the punctuation, so nothing shows where one sentence stops and the next begins. A video editor needs those points to cut the clip between sentences instead of through one. You get the words in order, each written as number:word. Return the numbers of the words that end a sentence or a complete thought.

How to decide:
- Read for meaning, as you would when adding full stops to the text. Spoken sentences are short: most are between 3 and 15 words.
- Several people often talk in turn or over each other. When the speaker seems to change, the previous speaker's sentence has ended.
- A short reaction that stands alone ("yeah exactly", "oh my god", "wait what") is a sentence of its own.
- Cover the whole transcript. If more than about 25 words pass with no end, look at that stretch again: there is almost always one.
- The transcript may begin or stop in the middle of a sentence. Mark the last word only if the thought is finished there.
- Words can be misheard. Work with what is written and never correct it: your answer holds only numbers.

## User template

{count} words, numbered 0 to {last}:

{words}
