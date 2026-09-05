import { useState } from 'react'
import type { BotConfig } from './types'

interface SettingsPanelProps {
  config: BotConfig | null
  onClose: () => void
  onSave: (config: Partial<BotConfig>) => Promise<void>
}

export function SettingsPanel({ config, onClose, onSave }: SettingsPanelProps) {
  const [walletAddress, setWalletAddress] = useState(config?.wallet_address || '')
  const [accountId, setAccountId] = useState(config?.account_id?.toString() || '')
  const [chainId, setChainId] = useState(config?.chain_id || 1729)
  const [apiUrl, setApiUrl] = useState(config?.api_url || 'https://api.reya.xyz/v2')
  const [wsUrl, setWsUrl] = useState(config?.ws_url || 'wss://ws.reya.xyz/')
  const [isActive, setIsActive] = useState(config?.is_active || false)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  const handleSave = async () => {
    setSaving(true)
    setSaved(false)
    await onSave({
      id: 1,
      wallet_address: walletAddress || null,
      account_id: accountId ? parseInt(accountId) : null,
      chain_id: chainId,
      api_url: apiUrl,
      ws_url: wsUrl,
      is_active: isActive,
    })
    setSaving(false)
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2 className="modal-title">Bot Configuration</h2>
          <button className="modal-close" onClick={onClose}>X</button>
        </div>

        <div className="modal-body">
          <div className="form-section">
            <div className="form-label">NETWORK</div>
            <div className="form-row">
              <label className="form-radio">
                <input
                  type="radio"
                  name="chain"
                  checked={chainId === 1729}
                  onChange={() => {
                    setChainId(1729)
                    setApiUrl('https://api.reya.xyz/v2')
                    setWsUrl('wss://ws.reya.xyz/')
                  }}
                />
                <span>Mainnet (1729)</span>
              </label>
              <label className="form-radio">
                <input
                  type="radio"
                  name="chain"
                  checked={chainId === 89346162}
                  onChange={() => {
                    setChainId(89346162)
                    setApiUrl('https://api-cronos.reya.xyz/v2')
                    setWsUrl('wss://websocket-testnet.reya.xyz/')
                  }}
                />
                <span>Testnet (89346162)</span>
              </label>
            </div>
          </div>

          <div className="form-section">
            <div className="form-label">WALLET ADDRESS</div>
            <input
              className="form-input"
              type="text"
              value={walletAddress}
              onChange={(e) => setWalletAddress(e.target.value)}
              placeholder="0x..."
            />
            <div className="form-hint">The Reya wallet address that owns the trading account.</div>
          </div>

          <div className="form-section">
            <div className="form-label">ACCOUNT ID</div>
            <input
              className="form-input"
              type="number"
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              placeholder="e.g. 123"
            />
            <div className="form-hint">The Reya account ID used for placing orders.</div>
          </div>

          <div className="form-section">
            <div className="form-label">API URL</div>
            <input
              className="form-input"
              type="text"
              value={apiUrl}
              onChange={(e) => setApiUrl(e.target.value)}
            />
          </div>

          <div className="form-section">
            <div className="form-label">WEBSOCKET URL</div>
            <input
              className="form-input"
              type="text"
              value={wsUrl}
              onChange={(e) => setWsUrl(e.target.value)}
            />
          </div>

          <div className="form-section">
            <label className="form-checkbox">
              <input
                type="checkbox"
                checked={isActive}
                onChange={(e) => setIsActive(e.target.checked)}
              />
              <span>Engine Active (allow autonomous strategy execution)</span>
            </label>
          </div>

          <div className="form-warning">
            The private key for signing orders is stored as a Supabase edge function secret (REYA_PRIVATE_KEY).
            It never touches the browser. Only the wallet address and account ID are stored in the database.
          </div>
        </div>

        <div className="modal-footer">
          {saved && <span className="save-confirm">SAVED</span>}
          <button className="btn-secondary" onClick={onClose}>CANCEL</button>
          <button className="btn-primary" onClick={handleSave} disabled={saving}>
            {saving ? 'SAVING...' : 'SAVE CONFIGURATION'}
          </button>
        </div>
      </div>
    </div>
  )
}
