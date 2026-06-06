# df-hlm-6-approval-tracker — Output [CRUX-MK]
*Autonom aktiviert 2026-06-05T14:12:04.585537+00:00 | ollama-local/qwen2.5:14b-instruct*

# Dark-Factory 'df-hlm-6-approval-tracker' Dokumentation

## Mission
Der Approval Tracker für die HeyLou Marketing Wellen 2 synchronisiert geneh
genehmigungsbedürftige Items mit der Salesforce Marketing Cloud und setzt E
Escalationsregeln ein.

## Aktivierungsmodi
* **Mock:** Standardmodus für Tests und Entwicklung.
* **Internal-Real:** Verbindet sich mit dem internen Salesforce Umfeld.
* **External-Real:** Verbindet sich mit dem externen Salesforce Produktions
Produktionsumfeld.

## K11-K16 Härtung
Der Fehler-Radius beträgt 1, Abhängigkeiten werden in eine separate DLQ (De
(Dead-Letter Queue) abgelegt. Provenance ist erforderlich und muss im Outpu
Output enthalten sein. Ein nicht-LLM Validierungsschicht ist zwingend erfor
erforderlich.

## Real-Modus-Trigger
Der Realmodus wird durch die Umgebungsvariablen `DF_HLM_6_REAL_SALESFORCE_E
`DF_HLM_6_REAL_SALESFORCE_ENABLED`, `SF_ENV` sowie den Phronesis Ticket `PH
`PHRONESIS_TICKET` aktiviert.

---

### Dokumentierte Codezustände und Evidenzen

#### API-Kontrakte
- **Internal-Real:** Verbindet sich mit dem internen Salesforce Umfeld, was
was auf die Aktivierung durch spezifische ENV-Vars und Phronesis Ticket abh
abhängt.
- **External-Real:** Schaltet den Workflow auf das Produktionsumfeld um.

#### Dataclass Felder
| Modul | Dataclass | Feld | Typ | Default |
| --- | --- | --- | --- | --- |
| src/approval_tracker.py | ApprovalItem | item_id | str |  |
| src/approval_tracker.py | ApprovalItem | status | ApprovalStatusEnum | `P
`Pending` |
| src/approval_tracker.py | ApprovalItem | escalation_level | int | 0 |

#### Umgebungsvariablen
- **DF_HLM_6_REAL_SALESFORCE_ENABLED:** Aktiviert den Zugriff auf das Sales
Salesforce Umfeld.
- **SF_ENV:** Bestimmt, ob der interne oder externe Salesforce Umfang verwe
verwendet wird.
- **PHRONESIS_TICKET:** Notwendig für die physische Durchführung im Produkt
Produktionsumfeld.

---

Diese Dokumentation dient als Ausgangspunkt für den Konfigurations- und Tes
Testprozess. Sie stellt sicher, dass alle notwendigen Komponenten korrekt e
eingerichtet sind und die Dark Factory 'df-hlm-6-approval-tracker' probleml
problemlos in verschiedenen Umgebungen operieren kann.

---

Dieses Dokument ist eine direkte Ausgabe der Dark-Factory 'df-hlm-6-approva
'df-hlm-6-approval-tracker', generiert um die Struktur, Zweck und den Betri
Betriebszustand des Systems im Kontext von HeyLou's Marketing Wellen 2 zu d
dokumentieren.