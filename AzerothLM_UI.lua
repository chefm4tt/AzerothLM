local addonName, ns = ...
local DB_NAME = "AzerothLM_DB"

-- ----------------------------------------------------------------------------
-- UI Logic
-- ----------------------------------------------------------------------------
local function HexToText(hex)
	if not hex then return "" end
	if #hex > 0 and #hex % 2 == 0 and string.match(hex, "^%x+$") then
		return (string.gsub(hex, "..", function (cc)
			return string.char(tonumber(cc, 16))
		end))
	end
	return hex
end

function AzerothLM_UpdateTerminalDisplay()
	local f = _G["AzerothLM_Frame"]
	if not f then return end

	local db = _G[DB_NAME]

	-- Load Button Visibility
	if f.loadBtn then
		if AzerothLM_Signal and type(AzerothLM_Signal) == "table" and db and db.currentChatID and AzerothLM_Signal.chatID == db.currentChatID then
			f.loadBtn:Show()
		else
			f.loadBtn:Hide()
		end
	end

	if f.status and db then
		if db.status == "SENT" then
			f.status:SetText("Status: Thinking...")
		else
			f.status:SetText("Status: Ready")
		end
	end

	-- Sync Button Visibility
	if f.syncBtn and f.input and db then
		local pendingQuery = f.input:GetText() ~= ""
		if db.status == "SENT" or pendingQuery then f.syncBtn:Show() else f.syncBtn:Hide() end
	end

	f.history:Clear()
	if not db or not db.chats or not db.currentChatID then return end
	
	local chat = db.chats[db.currentChatID]
	if not chat then return end

	for _, msg in ipairs(chat.messages) do
		local color = (msg.sender == "You") and "|cFF00FFFF" or "|cFF00FF00"
		local senderName = (msg.sender == "AI") and "ALM" or msg.sender
		local text = HexToText(msg.text)
		local lines = { strsplit("\n", text) }
		local headerPrinted = false
		for _, line in ipairs(lines) do
			if line and line ~= "" then
				if not headerPrinted then
					f.history:AddMessage(color .. senderName .. ":|r " .. line)
					headerPrinted = true
				else
					f.history:AddMessage(line)
				end
			end
		end
	end
end

function CreateAzerothLMFrame()
	local f = CreateFrame("Frame", "AzerothLM_Frame", UIParent, "BackdropTemplate")
	f:SetSize(550, 350)
	f:SetPoint("CENTER")
	f:SetBackdrop({
		bgFile = "Interface\\DialogFrame\\UI-DialogBox-Background",
		edgeFile = "Interface\\DialogFrame\\UI-DialogBox-Border",
		tile = true, tileSize = 32, edgeSize = 32,
		insets = { left = 11, right = 12, top = 12, bottom = 11 }
	})
	f:EnableMouse(true)
	f:SetMovable(true)
	f:RegisterForDrag("LeftButton")
	f:SetScript("OnDragStart", f.StartMoving)
	f:SetScript("OnDragStop", f.StopMovingOrSizing)

	-- Title
	f.title = f:CreateFontString(nil, "OVERLAY", "GameFontNormal")
	f.title:SetPoint("TOP", 0, -12)
	f.title:SetText("AzerothLM Terminal")

	-- Close Button
	f.closeBtn = CreateFrame("Button", nil, f, "UIPanelCloseButton")
	f.closeBtn:SetPoint("TOPRIGHT", -2, -2)

	-- Sync Button
	f.syncBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
	f.syncBtn:SetSize(50, 20)
	f.syncBtn:SetPoint("RIGHT", f.closeBtn, "LEFT", -5, 0)
	f.syncBtn:SetText("Sync")
	f.syncBtn:RegisterForClicks("LeftButtonUp", "RightButtonUp")
	f.syncBtn:SetScript("OnClick", function(self, button)
		if button == "RightButton" then
			if AzerothLM_ForceReset then AzerothLM_ForceReset() end
			return
		end

		local db = _G[DB_NAME]
		if db then
			db.lastSyncTime = GetTime()
		end
		print("|cFF00FF00AzerothLM|r: Syncing with AI...")
		ReloadUI()
	end)

	-- Load Button
	f.loadBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate, BackdropTemplate")
	f.loadBtn:SetSize(50, 20)
	f.loadBtn:SetPoint("RIGHT", f.syncBtn, "LEFT", -5, 0)
	f.loadBtn:SetText("Load")
	f.loadBtn:SetBackdrop({
		bgFile = "Interface\\Buttons\\WHITE8x8",
		edgeFile = "Interface\\Buttons\\UI-SliderBar-Border",
		tile = true, tileSize = 8, edgeSize = 8,
		insets = { left = 3, right = 3, top = 3, bottom = 3 }
	})
	f.loadBtn:SetBackdropColor(0, 1, 0, 1)
	f.loadBtn:SetScript("OnClick", function() AzerothLM_ManualPull() end)
	f.loadBtn:Hide()

	-- Status Label
	f.status = f:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
	f.status:SetPoint("RIGHT", f.loadBtn, "LEFT", -5, 0)
	f.status:SetText("Status: Ready")

	-- Sidebar
	f.sidebar = CreateFrame("Frame", nil, f, "BackdropTemplate")
	f.sidebar:SetPoint("TOPLEFT", 12, -12)
	f.sidebar:SetPoint("BOTTOMLEFT", 12, 12)
	f.sidebar:SetWidth(130)
	f.sidebar:SetBackdrop({
		bgFile = "Interface\\DialogFrame\\UI-DialogBox-Background",
		edgeFile = "Interface\\Tooltips\\UI-Tooltip-Border",
		tile = true, tileSize = 16, edgeSize = 16,
		insets = { left = 4, right = 4, top = 4, bottom = 4 }
	})

	-- Clear Button
	f.clearBtn = CreateFrame("Button", nil, f.sidebar, "UIPanelButtonTemplate")
	f.clearBtn:SetSize(120, 20)
	f.clearBtn:SetPoint("BOTTOM", 0, 6)
	f.clearBtn:SetText("Clear")
	f.clearBtn:SetScript("OnClick", function()
		local db = _G[DB_NAME]
		if IsShiftKeyDown() then
			print("|cFF00FF00AzerothLM|r: Full database reset performed.")
			if db then
				db.chats = {}
				table.insert(db.chats, { name = "General", messages = {} })
				db.currentChatID = 1
				f:RefreshTabs()
				AzerothLM_UpdateTerminalDisplay()
			end
			return
		end
		if db and db.chats and db.currentChatID and db.chats[db.currentChatID] then
			-- Only clear messages, preserving gear/quests/professions context
			db.chats[db.currentChatID].messages = {}
			table.insert(db.chats[db.currentChatID].messages, { sender = "System", text = "History cleared for this chat." })
			AzerothLM_UpdateTerminalDisplay()
		end
	end)

	-- Rename Box
	f.renameBox = CreateFrame("EditBox", nil, f.sidebar, "InputBoxTemplate")
	f.renameBox:SetPoint("TOPLEFT", 8, -5)
	f.renameBox:SetSize(90, 20)
	f.renameBox:SetAutoFocus(false)
	f.renameBox:SetScript("OnEnterPressed", function(self)
		local text = self:GetText()
		local db = _G[DB_NAME]
		if db and db.chats and db.currentChatID and db.chats[db.currentChatID] then
			if not text or text == "" then
				text = "New Chat"
			end
			db.chats[db.currentChatID].name = text
			f:RefreshTabs()
			self:SetText("")
			self:ClearFocus()
		end
	end)

	-- Scrollable Chat History
	f.history = CreateFrame("ScrollingMessageFrame", "AzerothLM_Terminal", f)
	f.history:SetPoint("TOPLEFT", f.sidebar, "TOPRIGHT", 10, -28)
	f.history:SetPoint("BOTTOMRIGHT", -20, 50)
	f.history:SetFontObject("ChatFontNormal")
	f.history:SetJustifyH("LEFT")
	f.history:SetFading(false)
	f.history:SetMaxLines(1000)
	f.history:SetInsertMode("BOTTOM")
	f.history:AddMessage("Welcome to AzerothLM.")

	-- Input Box
	f.input = CreateFrame("EditBox", nil, f, "InputBoxTemplate")
	f.input:SetPoint("BOTTOMLEFT", f.sidebar, "BOTTOMRIGHT", 10, 8)
	f.input:SetPoint("BOTTOMRIGHT", -20, 20)
	f.input:SetHeight(20)
	f.input:SetAutoFocus(false)
	f.input:SetScript("OnEnterPressed", function(self)
		local text = self:GetText()
		if text and text ~= "" then
			local db = _G[DB_NAME]
			if db then
				if AzerothLM_UpdatePlayerContext then
					AzerothLM_UpdatePlayerContext(true)
				end
				db.query = text
				db.status = "SENT"
				db.response = nil
				if f.status then f.status:SetText("Status: Thinking...") end
				if db.currentChatID and db.chats[db.currentChatID] then
					table.insert(db.chats[db.currentChatID].messages, { sender = "You", text = text })
					table.insert(db.chats[db.currentChatID].messages, { sender = "System", text = "|cffffff00Message queued. Click the red Sync button to process.|r" })
				end
				AzerothLM_UpdateTerminalDisplay()
			end
			self:SetText("")
			self:ClearFocus()
		end
	end)

	f.tabButtons = {}
	
	function f:RefreshTabs()
		local db = _G[DB_NAME]
		if not db or not db.chats then return end

		if f.renameBox and db.currentChatID and db.chats[db.currentChatID] then
			f.renameBox:SetText(db.chats[db.currentChatID].name)
		end

		-- Clear existing
		for _, btn in pairs(f.tabButtons) do btn:Hide() end

		-- New Chat Button
		if not f.newChatBtn then
			f.newChatBtn = CreateFrame("Button", nil, f.sidebar, "GameMenuButtonTemplate")
			f.newChatBtn:SetText("+")
			f.newChatBtn:SetSize(20, 20)
			f.newChatBtn:SetPoint("TOPRIGHT", -5, -5)
			f.newChatBtn:SetScript("OnClick", function()
				table.insert(db.chats, { name = "Chat " .. (#db.chats + 1), messages = {} })
				db.currentChatID = #db.chats
				f:RefreshTabs()
				AzerothLM_UpdateTerminalDisplay()
			end)
		end

		local yOffset = -35
		for i, chat in ipairs(db.chats) do
			local btn = f.tabButtons[i]
			if not btn then
				btn = CreateFrame("Button", nil, f.sidebar, "GameMenuButtonTemplate")
				btn:SetSize(100, 20)
				
				btn.delBtn = CreateFrame("Button", nil, btn, "GameMenuButtonTemplate")
				btn.delBtn:SetText("x")
				btn.delBtn:SetSize(16, 16)
				btn.delBtn:SetPoint("RIGHT", btn, "RIGHT", 2, 0)
				btn.delBtn:SetScript("OnClick", function()
					tremove(db.chats, i)
					if db.currentChatID >= i and db.currentChatID > 1 then
						db.currentChatID = db.currentChatID - 1
					end
					if #db.chats == 0 then
						table.insert(db.chats, { name = "General", messages = {} })
						db.currentChatID = 1
					end
					f:RefreshTabs()
					AzerothLM_UpdateTerminalDisplay()
				end)

				btn:SetScript("OnClick", function()
					db.currentChatID = i
					f:RefreshTabs()
					AzerothLM_UpdateTerminalDisplay()
				end)
				f.tabButtons[i] = btn
			end

			btn:SetPoint("TOP", f.sidebar, "TOP", -10, yOffset)
			btn:SetText(chat.name)
			btn:Show()
			
			if i == db.currentChatID then
				btn:LockHighlight()
			else
				btn:UnlockHighlight()
			end
			yOffset = yOffset - 25
		end
	end

	f:SetScript("OnShow", function()
		f:RefreshTabs()
		AzerothLM_UpdateTerminalDisplay()
	end)

	f:RegisterEvent("PLAYER_ENTERING_WORLD")
	f:SetScript("OnEvent", function(self, event)
		if event == "PLAYER_ENTERING_WORLD" then
			AzerothLM_UpdateTerminalDisplay()
		end
	end)

	local timeSinceLastUpdate = 0
	f:SetScript("OnUpdate", function(self, elapsed)
		timeSinceLastUpdate = timeSinceLastUpdate + elapsed
		if timeSinceLastUpdate >= 2 then
			timeSinceLastUpdate = 0
			AzerothLM_UpdateTerminalDisplay()
		end
	end)

	f:Hide()
end

function AzerothLM_PrintResponse(response)
	if not _G["AzerothLM_Frame"] then
		CreateAzerothLMFrame()
	end
	local f = _G["AzerothLM_Frame"]
	
	local db = _G[DB_NAME]
	if db and db.currentChatID and db.chats[db.currentChatID] then
		table.insert(db.chats[db.currentChatID].messages, { sender = "AI", text = response })
	end
	
	f:Show()
	AzerothLM_UpdateTerminalDisplay()
end