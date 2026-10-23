//
//  IPView.swift
//  BoatData
//
//  Created by Paul Viola on 3/17/24.
//

import Foundation
import SwiftUI


struct IPAddressView: View {
    @ObservedObject var updater: IpAddressUpdater
    var body: some View {
        VStack {
            Text("IP Address: \(updater.ipAddress.address())")
                .font(.title2)
            HStack {
                ForEach(0..<4, id: \.self) { index in
                    Picker("", selection: $updater.ipAddress.segments[index]) {
                        ForEach(0...255, id: \.self) { number in
                            Text("\(number)").tag(number)
                        }
                    }
                    .frame(minWidth: 150)
                    .pickerStyle(WheelPickerStyle())
                    .frame(width: 50, height: 150)
                    .clipped()
                    .compositingGroup()
                    
                    if index < 3 {
                        Text(".")
                            .font(.title2)
                            .foregroundColor(.gray)
                    }
                }
            }
            Button("Save & Connect") {
                // Save to UserDefaults
                UserDefaults.standard.set(updater.ipAddress.address(), forKey: ConfigKeys.address)
                updater.resetAddress()
            }
        }
    }
}


struct IPAddressView_Previews: PreviewProvider {
    static var previews: some View {
        IPAddressView(updater: IpAddressUpdater(ipAddress: IpAddress(), dataManager: DataManager()))
    }
}
