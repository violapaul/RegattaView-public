
# ActisenseRelay 

- Listens on the serial bus for actisense messages that were logged on the boat using the RPi.
- Parses them into N2K messages.
- Resends them on the CAN bus.
- Also processes each message locally.  This is used to confirm that the processing on the downstream board is valid.
    - Smooths signals and computes true wind.

