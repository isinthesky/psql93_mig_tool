# Project Plan: UI Styling Improvements

## Objective
Increase button size, font size, and add padding globally across the application to improve usability and aesthetics.

## Tasks
1.  **Analyze Existing Styles**: Locate where `QApplication` is initialized and if any existing styles are applied.
2.  **Implement Global Stylesheet**: Apply a QSS (Qt Style Sheet) to the `QApplication` instance targeting `QPushButton`.
    -   Target properties: `padding`, `font-size`, `min-height`, `min-width`.
3.  **Verify**: Ensure the styles cascade correctly to dialogs and main window.

