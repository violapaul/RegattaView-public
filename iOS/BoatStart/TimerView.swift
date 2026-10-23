//
//  TimerView.swift
//  BoatTimer
//
//  Created by Paul Viola on 12/4/22.
//

import SwiftUI

struct TimerButtonView: View {
    var text: String
    var onTap: () -> Void
    @GestureState private var isTapped = false
    
    var body: some View {
        
        let drag = DragGesture(minimumDistance: 0)
            .updating($isTapped) { (_, isTapped, _) in
                isTapped = true
                print(text)
                Alert.buttonPress()
                onTap()
            }

        ZStack {
            SwiftUI.Text(text)
            Rectangle()
                .stroke(.red)
                .contentShape(Rectangle())
                .gesture( drag ) //makeTap(callBack: onTap))
        }
    }
}

struct TimerView: View {
    
    @ObservedObject var timeManager: TimeManager
    
    static let clockPt: CGFloat = 260
    
    let clockFont = Font
        .system(size: TimerView.clockPt)
        .monospacedDigit()

    let buttonFont = Font
        .system(size: 50)
        .monospacedDigit()
    
    var body: some View {

        VStack (alignment: .trailing, spacing: 0.0) {
            HStack (spacing: 0.0) {
                TimerButtonView(text: "+1", onTap: {
                    timeManager.timeDelta(seconds: 60.0)
                })
                TimerButtonView(text: "Gun", onTap: {
                    timeManager.gun()
                })
                TimerButtonView(text: "-1", onTap: {
                    timeManager.timeDelta(seconds: -60.0)
                })
            }
            .font(buttonFont)
            .zIndex(1.0)
            // nSpacer(minLength: 250.0)
            HStack {
                Spacer()
                if timeManager.timeToStart >= 0 {
                    Text(timeManager.timeString)
                        .font(clockFont)
                        .frame(height: TimerView.clockPt * 0.85)
                } else {
                    Text(timeManager.timeString)
                        .font(clockFont)
                        .frame(height: TimerView.clockPt * 0.85)
                        .foregroundColor(.green)
                }
                Spacer()
            }
            .zIndex(0.0)
            HStack (spacing: 0.0) {
                TimerButtonView(text: "Sync", onTap: {
                    timeManager.sync()
                })
                TimerButtonView(text: "Rolling", onTap: { print("Rolling")})
                TimerButtonView(text: "Reset", onTap: {
                    timeManager.reset()
                })
            }
            .font(buttonFont)
            .zIndex(1.0)
        }
        .padding(-10)
        .ignoresSafeArea()
    }
}


struct TimerView_Previews: PreviewProvider {
    static var previews: some View {
        TimerView(timeManager: TimeManager())
            .previewInterfaceOrientation(.landscapeLeft)
    }
}

