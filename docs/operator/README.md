# Operator work instructions

The screens show these steps at each stage. This page is the printable version. **[NOT VERIFIED]** layout against a customer's own work-instruction template: adapt the header block to their quality system.

## Applicable standards

Cited from general knowledge. **The standards themselves were not opened** while writing this, so check the clause numbers against the editions the customer works to:

| Standard | Clause | Why it applies |
|---|---|---|
| ISO 9001:2015 | 7.1.5 Monitoring and measuring resources | the stand is a measuring resource; the Golden OK / Reject check at the start of each part is its verification before use |
| ISO 9001:2015 | 7.5 Documented information | these instructions, the audit log, the stored results |
| ISO 9001:2015 | 8.5.1 Control of production | work instructions available at the workstation; monitoring at the right stage |
| IATF 16949:2016 | 8.5.1.2 Standardised work: operator instructions and visual standards | instructions at the station, in the operator's language, visible during the work |
| IATF 16949:2016 | 7.1.5.1.1 Measurement system analysis | the cross-validated escape and false-reject rates are the stand's MSA evidence for attribute decisions |

## Document header (fill in per customer)

| Field | Value |
|---|---|
| Station | EOL vibro-acoustic test stand |
| Part numbers | from Setup |
| Revision / date | |
| Approved by | |

## A. Start of a part (calibration)

1. Log in with your own user name.
2. Testing → choose the part number.
3. Put the marked **Golden OK** reference part in the fixture. Run it as for a normal test. Press **Test Golden OK**. It must show **OK**.
4. Put the marked **known Reject** part in the fixture. Press **Test Reject**. It must show **NOK** or **UNDEFINED PRODUCT**.
5. Only then is testing open. If either check fails: testing stays blocked. Check the part, the fixture and the microphone, and call the setter.

Do it again after every change of part number, and at the start of every shift.

## B. Testing a part

1. Scan or type the unit's serial.
2. Put the part in the fixture and run it the same way every time.
3. Press **Test** and wait for the result. Do not touch the fixture or the microphone during the capture.
4. Act on the result:

| Screen | Meaning | What to do |
|---|---|---|
| **OK** (green) | the part matches the Golden OK part | pass it on |
| **NOK: \<mode\>** (red) | the part shows that failure mode | put it in the reject area, labelled with the mode |
| **UNDEFINED PRODUCT** (purple) | the part matches neither Golden OK nor any known failure mode | put it in the quarantine area; tell the setter. It may be a new failure mode |
| **INVALID** (grey) | the capture could not be judged (the reason is shown) | fix the cause shown, then test the part again (tick "retest") |
| **CONTROLLED DRIFT** (yellow, with OK) | the part passed, but closer to the limit than usual | pass it on; tell the setter if it repeats |

5. A retest is your decision. Tick "This is a retest of the last part" so the reports count it as one.

## C. Never

- Never change the microphone position or the fixture between the calibration and the tests.
- Never test with the emergency exit pressed. **The emergency exit is NOT a safety function**: it only shuts the computer down in an orderly way.
