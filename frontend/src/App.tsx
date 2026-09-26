import { useState } from 'react'
import ProjectsScreen from './ProjectsScreen'
import ToolsPanel from './ToolsPanel'
import './App.css'

type Tab = 'projects' | 'tools'

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
            Projects
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

      <main>{tab === 'projects' ? <ProjectsScreen /> : <ToolsPanel />}</main>
    </>
  )
}

export default App
