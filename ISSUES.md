# Wodex Issues

- [x] When the user hits enter in the Wodex prompt, it should clear the prompt field and insert that into the chat log.  (currently it doesn't clear the prompt field until Codex returns).

- [x] While Codex is thinking put a timer next to "Thinking..."  That counts up like "Thinking... 41s" or "3m12s" updated every second since the last assistant entry in the chat log.  As new assistant entries come in it should insert them into the chat log as soon as they come in and reset the timer.

- [x] If the user enters a prompt when a turn is complete send a turn/start.  If the user enters a prompt while a turn is processing, send a turn/steer.

- [x] Make it so that the chat log entry for an in flight agentMessage is updated dynamically with item/agentMessage/delta as it comes in.
