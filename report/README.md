# Final report

`deliverables/159333_Final_Report_Bargain_Agent.docx` (and `.pdf`) is generated from `build_report.js`.

```
cd report
npm install
node build_report.js ../deliverables/159333_Final_Report_Bargain_Agent.docx
powershell -File render.ps1      # Word updates the table of contents and exports the PDF
```

`fig/capture.py` re-takes the screenshots (it starts the app on port 8767 and runs one real search).
Text marked with a yellow background in the report is for the team to fill in.
