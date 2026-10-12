import { useState } from 'react'
import LibraryScreen from './LibraryScreen'
import ProjectsScreen from './ProjectsScreen'
import ToolsPanel from './ToolsPanel'
import './App.css'

type Tab = 'projects' | 'library' | 'tools'

function App() {
  const [tab, setTab] = useState<Tab>('projects')

  return (
    <>
      <header className="app-header">
        <h1>Video Factory</h1>
        <nav className="row">
          <button
            type="button"
            className={tab === 'projects' ? 'tab active' : 'tab'}
            onClick={() => setTab('projects')}
          >
            Videos
          </button>
          <button
            type="button"
            className={tab === 'library' ? 'tab active' : 'tab'}
            onClick={() => setTab('library')}
          >
            Asset library
          </button>
          <button
            type="button"
            className={tab === 'tools' ? 'tab active' : 'tab'}
            onClick={() => setTab('tools')}
          >
            Tools
          </button>
        </nav>
      </header>

      <main>
        {tab === 'projects' && <ProjectsScreen />}
        {tab === 'library' && <LibraryScreen />}
        {tab === 'tools' && <ToolsPanel />}
      </main>
    </>
  )
}

export default App
