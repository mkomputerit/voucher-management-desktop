"""Plain-language report selection, shared by the dialog and its help panel."""
from .reporting import ReportKind

REPORT_GUIDE = {
    ReportKind.SUMMARY: ("Voglio una panoramica generale", "Totali dello storico conservato. I dati mancanti sono indicati separatamente; zero non significa sempre assenza di voucher."),
    ReportKind.GENERATED: ("Quali voucher abbiamo creato con il software?", "Solo creazioni confermate da questa applicazione. I vecchi voucher senza questa informazione non sono inclusi."),
    ReportKind.GENERATED_UNUSED: ("Quali voucher creati non risultano utilizzati?", "Creazioni confermate con dati di utilizzo disponibili e nessun uso rilevato. Origine o utilizzo sconosciuti non soddisfano il filtro."),
    ReportKind.USED: ("Quali voucher risultano utilizzati?", "Almeno un utilizzo rilevato nelle sincronizzazioni conservate, anche se il contatore successivamente torna a zero."),
    ReportKind.EXPIRED: ("Quali voucher risultano scaduti?", "Scadenza dai dati del controller o dalla data di scadenza conservata. Le sole registrazioni importate da backup non provano la scadenza."),
    ReportKind.PRINTED: ("Quali voucher hanno stampe registrate?", "Stampe registrate nell'archivio locale, comprese le evidenze importate. Aprire o salvare un PDF non registra una stampa voucher."),
    ReportKind.PRINTED_UNUSED: ("Quali stampati non risultano utilizzati?", "Richiede stampa e dati di utilizzo sulla stessa identità voucher. Le stampe storiche senza collegamento verificato al controller restano escluse."),
    ReportKind.NEVER_PRINTED: ("Quali voucher non hanno stampe registrate?", "Nessuna stampa associata a queste registrazioni locali. Non dimostra che il voucher non sia stato stampato altrove o prima dell'importazione."),
    ReportKind.NOMINAL: ("Quali voucher sono nominali?", "Solo voucher con il flag Voucher nominale. Il nome del destinatario non determina la classificazione; i vecchi voucher restano non classificati."),
    ReportKind.FULL_HISTORY: ("Devo ricostruire lo storico", "Dettaglio amministrativo conservato, inclusi dati importati e archiviati. Registrazioni con nomi uguali non sono necessariamente lo stesso voucher."),
    ReportKind.UNCLASSIFIED: ("Verifica dati: nominalità mancante", "Voucher non ancora classificati come nominali o non nominali."),
    ReportKind.USAGE_UNKNOWN: ("Verifica dati: utilizzo sconosciuto", "Non ci sono dati sufficienti per stabilire l'utilizzo. Non vanno contati come inutilizzati. Sincronizzare aggiorna i voucher ancora identificabili sul controller."),
    ReportKind.ORIGIN_UNKNOWN: ("Verifica dati: origine sconosciuta", "Manca una conferma di creazione tramite questa applicazione. Una sincronizzazione non ricostruisce chi abbia creato un vecchio voucher."),
    ReportKind.NOMINALITY_REDACTED: ("Verifica dati: nominalità rimossa", "Classificazione rimossa dalla procedura di conservazione per privacy. Non viene ricostruita dal destinatario."),
}

HISTORY_NOTICE = (
    "I report utilizzano lo storico salvato nel software e sono disponibili anche senza "
    "connessione al controller. Sincronizza con UniFi per aggiornare i dati di utilizzo. "
    "La connessione non comporta un aggiornamento continuo."
)
