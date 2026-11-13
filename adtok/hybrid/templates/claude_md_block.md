## Model routing

- Exploration that spans more than a couple of files: use the `Explore` agent (Haiku). Do small lookups yourself.
- Independent, well-scoped implementation pieces, especially several that can run in parallel: delegate to `implementer` (Sonnet) with the files, the change and how to verify it.
- A bug with a known failing test or error: `fixer` (Sonnet, high effort).
- Design across modules, plans for large changes, and reviews of finished work: `architect` (Opus, read-only).
- Keep planning, integration, and any step that depends on what you have already read in this conversation here. Do not split a dependent chain of steps across agents.
- A `Task tier:` note may be attached to a prompt by a hook. Treat it as advice, not an order.
- After a delegated change, run its tests yourself. If they fail, retry at a higher tier (`implementer` -> do it yourself; `low`/`medium` -> `high`) instead of repeating the same attempt.
