"""Streamlit demo + analyst review UI (module 5; thin slice earlier for manual testing).

Tabs:
  Run case      pipeline execution with live ReAct trace (Thought -> Action -> Observation)
  Packet        assembled case file: worksheet, title report, cited compliance note
  Emails        inbox / outbox threads (MockEmailClient) + "Simulate customer reply"
  Review        approve / return-with-reason-codes (feeds the return tally store)
  Audit trail   every action + its source

Run:  streamlit run src/settleready/ui/app.py
"""
from __future__ import annotations


def main() -> None:
    raise NotImplementedError("TODO(module 5): build the Streamlit UI.")


if __name__ == "__main__":
    main()
