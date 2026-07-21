import EventLog from "./components/EventLog";
import GroupBoard from "./components/GroupBoard";
import { sampleEscalations, sampleEventLog, sampleRunState } from "./sample-run";

function App() {
  return (
    <div className="app">
      <header className="app__header">
        <h1>Orchestrator Dashboard</h1>
        <p className="app__run-id">run: {sampleRunState.run_id}</p>
      </header>
      <GroupBoard runState={sampleRunState} />
      <EventLog lines={sampleEventLog} escalations={sampleEscalations} />
    </div>
  );
}

export default App;
