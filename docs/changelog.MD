**Changes to existing tabs:**

- **Extracted Definitions** gets the biggest upgrade. Next to each class and method you'll see how often it runs, when it last ran, how fast it is, and how often it fails. New filters let you find code that never runs (possibly dead) or runs a lot (hot). Opening a method shows who actually calls it, which pages or routes lead to it, and which database tables it uses.
- **Chat** learns to answer runtime questions, like "is this method still used?", "what uses the orders table?", or "what might break if I change this?". Search results in chat also show usage info next to the code.
- **Database Schema** shows how busy each table is (reads and writes), with busy tables highlighted. Clicking a table lists the code and routes that use it.
- **Knowledge Base (Files)** shows, for each project, whether it's running in Docker right now, and warns you if the running code differs from the code you uploaded.
- **Settings** gets a new "Runtime telemetry" card for the connection details, how long data is kept, and how much of the traffic is recorded.
- **Domain Knowledge** comes later: its entities and behaviors will show usage numbers too.

**One new tab, Runtime,** is the control center for live data:

- Which Docker containers are running and which project each one belongs to.
- Whether data is flowing in properly, and how much of it could be matched to your code.
- A dead-code report listing code that hasn't run in a while.
- A list of routes and the database tables each one touches.

**Two small general improvements:**

- Each tab will have its own URL, so you can share a link that opens directly on a specific method, for example from a GitLab merge request comment.
- The upload flow will ask for (or detect) which project and Git commit a ZIP belongs to, so live data can be matched to the right code.