# AI prompt log

Tool: Cursor, Grok 4.7. Times are local (UTC+5:30) on 6 Nov 2026.

## 15:11 — first prompt, unedited

C:\Users\User\Downloads\Kill It and Prove It.pdf see c this is my first hackathon please guide me and do everything by you and make me win this and also most important whatever you do give a correct explanation like you guide a child and help us with every because you know judge will ask every question and i want every explanation and also help with making a document with every explanation of every code. please help us chagpt you are my saviour  and my guidenance like a professional tacher and also like my friend explananing every detail
Or anything so please don't keep anything and give us the details and step by step guidance which lead us to win.
And also provide a explanation document , like the code explanation for judge to understand what concept we know.
I mean whatwver judges ask us we should know about it..
Go on ! I trust you.. let's go!!
Make a entire pdf!! For it don't waste your limits or anything. I want entire thing in whole damn pdf without gatekeeping anything explain everything.ROLE AND MISSION
Act as our senior software architect, Python systems engineer, database engineer, crash-consistency specialist, QA engineer and patient programming teacher.
We are a beginner student team participating in a 24-hour hackathon called "Kill It and Prove It".
Our challenge is to build a crash-safe key-value database from scratch.
We are beginners. We need you to guide us from an empty folder to a fully implemented, tested, documented and demonstrable submission.
You are not just a code generator. You must teach us, explain your decisions and help us prove correctness.

The rest of that message set the technology rules (Python standard library, os.fsync, one global lock, no SQLite), asked for a requirements breakdown before code, and said to wait for architecture approval. The full message is the first user message of this Cursor chat.

## 15:20 — second prompt, unedited

will you?

## 15:40 — third prompt, unedited

go

## 15:57 — fourth prompt, unedited

next?

## 17:13 — fifth prompt, unedited

go

## 17:16 — sixth prompt, unedited

go

## 18:05 — seventh prompt, unedited

go

## Decisions after those prompts

We read the official 19-page PDF before choosing a design. We did not start the engine until the team approved an append-only commit log. The team said the clock had started and chose the folder `C:\Users\User\OneDrive\Desktop\LogicRush`.

The first implementation used one frame per operation and two `os.fsync` calls. Replaying that log was too slow for 1,000,000 keys. The current file is version 2: one `TXN1` block per batch, one checksum of the whole body, one `os.fsync`, then the in-memory dictionary is updated. Compaction writes one `SNP1` block and swaps it in with `os.replace`.
