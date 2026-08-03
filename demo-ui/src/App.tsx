import { AppHeader } from "./components/AppHeader";
import { DocumentPanel } from "./components/DocumentPanel";
import { QaConsole } from "./components/QaConsole";
import { useDocuments } from "./hooks/useDocuments";
import { useHealth } from "./hooks/useHealth";
import styles from "./App.module.css";

export default function App() {
  const health = useHealth();
  const documents = useDocuments();

  return (
    <div className={styles.page}>
      <AppHeader health={health} />
      <main className={styles.main}>
        <DocumentPanel documents={documents} />
        <QaConsole />
      </main>
    </div>
  );
}
