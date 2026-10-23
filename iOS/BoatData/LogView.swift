//
//  LogView.swift
//  BoatData
//
//  Created by Claude on 4/14/26.
//

import SwiftUI

struct LogView: View {
    @State private var logText: String = "Loading..."
    @State private var autoRefresh = true
    private let timer = Timer.publish(every: 2, on: .main, in: .common).autoconnect()

    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("App Logs")
                    .font(.headline)
                Spacer()
                Toggle("Auto", isOn: $autoRefresh)
                    .labelsHidden()
                    .scaleEffect(0.8)
                Text("Auto")
                    .font(.caption)
                Button("Refresh") {
                    refreshLogs()
                }
                .font(.caption)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Color.blue.opacity(0.3))
                .cornerRadius(4)
                Button("Clear") {
                    FileLog.shared.clearLogs()
                    logText = "(cleared)"
                }
                .font(.caption)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Color.red.opacity(0.3))
                .cornerRadius(4)
            }
            .padding()
            .background(Color.black.opacity(0.3))

            // Log content
            ScrollViewReader { proxy in
                ScrollView {
                    Text(logText)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundColor(.white)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                        .id("logBottom")
                }
                .onChange(of: logText) { _ in
                    withAnimation {
                        proxy.scrollTo("logBottom", anchor: .bottom)
                    }
                }
            }
        }
        .onAppear {
            refreshLogs()
        }
        .onReceive(timer) { _ in
            if autoRefresh {
                refreshLogs()
            }
        }
    }

    private func refreshLogs() {
        logText = FileLog.shared.readLogs(maxLines: 300)
        if logText.isEmpty {
            logText = "(no logs yet)"
        }
    }
}

struct LogView_Previews: PreviewProvider {
    static var previews: some View {
        LogView()
            .preferredColorScheme(.dark)
    }
}
