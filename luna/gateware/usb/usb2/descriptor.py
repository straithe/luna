#
# This file is part of LUNA.
#
# Copyright (c) 2020 Great Scott Gadgets <info@greatscottgadgets.com>
# SPDX-License-Identifier: BSD-3-Clause
""" Utilities for building USB descriptors into gateware. """

import unittest

from nmigen                                  import Signal, Module, Elaboratable
from usb_protocol.emitters.descriptors       import DeviceDescriptorCollection
from usb_protocol.types.descriptors.standard import StandardDescriptorNumbers

from ..stream                          import USBInStreamInterface
from ...stream.generator               import ConstantStreamGenerator
from ...test                           import LunaUSBGatewareTestCase, usb_domain_test_case


class USBDescriptorStreamGenerator(ConstantStreamGenerator):
    """ Specialized stream generator for generating USB descriptor constants. """

    def __init__(self, data):
        """
        Parameters:
            descriptor_number -- The descriptor number represented.
            data              -- The raw bytes (or equivalent) for the descriptor.
        """

        # Always create USB descriptors in the USB domain; always have a maximum length field that can
        # be up to 16 bits wide, and always use USBInStream's. These allow us to tie easily to our request
        # handlers.
        super().__init__(data, domain="usb", stream_type=USBInStreamInterface, max_length_width=16)



class GetDescriptorHandler(Elaboratable):
    """ Gateware that handles responding to GetDescriptor requests.

    Currently does not support descriptors in multiple languages.

    I/O port:
        I: value[16]  -- The value field associated with the Get Descriptor request.
                         Contains the descriptor type and index.
        I: length[16] -- The length field associated with the Get Descriptor request.
                         Determines the maximum amount allowed in a response.

        I: start      -- Strobe that indicates when a descriptor should be transmitted.

        *: tx         -- The USBInStreamInterface that streams our descriptor data.
        O: stall      -- Pulsed if a STALL handshake should be generated, instead of a response.
    """

    def __init__(self, descriptor_collection: DeviceDescriptorCollection, max_packet_length=64):
        """
        Parameteres:
            descriptor_collection -- The DeviceDescriptorCollection containing the descriptors
                                     to use for this device.
        """

        self._descriptors = descriptor_collection
        self._max_packet_length = max_packet_length

        #
        # I/O port
        #
        self.value          = Signal(16)
        self.length         = Signal(16)

        self.start          = Signal()
        self.start_position = Signal(11)

        self.tx             = USBInStreamInterface()
        self.stall          = Signal()


    def elaborate(self, platform):
        m = Module()

        # Collection that will store each of our descriptor-generation submodules.
        descriptor_generators = {}

        #
        # Figure out the maximum length we're willing to send.
        #
        length = Signal(16)

        # We'll never send more than our MaxPacketSize. This means that we'll want to send a maximum of
        # either our maximum packet length, or the amount of data we have remaining; whichever is less.
        #
        # Note that this doesn't take into account the length of the actual data to be sent; this is handled
        # in the stream generator.
        words_remaining = self.length - self.start_position
        with m.If(words_remaining <= self._max_packet_length):
            m.d.comb += length.eq(words_remaining)
        with m.Else():
            m.d.comb += length.eq(self._max_packet_length)


        #
        # Create our constant-stream generators for each of our descriptors.
        #
        for type_number, index, raw_descriptor in self._descriptors:
            # Create the generator...
            generator = USBDescriptorStreamGenerator(raw_descriptor)
            descriptor_generators[(type_number, index)] = generator

            m.d.comb += [
                generator.max_length     .eq(length),
                generator.start_position .eq(self.start_position)
            ]

            # ... and attach it to this module.
            type_ref =  type_number.name if isinstance(type_number, StandardDescriptorNumbers) else type_number
            setattr(m.submodules, f'USBDescriptorStreamGenerator({type_ref},{index})', generator)


        #
        # Connect up each of our generators.
        #

        with m.Switch(self.value):

            # Generate a conditional interconnect for each of our items.
            for (type_number, index), generator in descriptor_generators.items():

                # If the value matches the given type number...
                with m.Case(type_number << 8 | index):

                    # ... connect the relevant generator to our output.
                    m.d.comb += generator.stream  .attach(self.tx)
                    m.d.usb += generator.start    .eq(self.start),

            # If none of our descriptors match, stall any request that comes in.
            with m.Case():
                m.d.comb += self.stall.eq(self.start)


        return m


class GetDescriptorHandlerTest(LunaUSBGatewareTestCase):
    descriptors = DeviceDescriptorCollection()

    with descriptors.DeviceDescriptor() as d:
        d.bcdUSB             = 2.00
        d.idVendor           = 0x1234
        d.idProduct          = 0x4567
        d.iManufacturer      = "Manufacturer"
        d.iProduct           = "Product"
        d.iSerialNumber      = "ThisSerialNumberIsResultsInADescriptorLongerThan64Bytes"
        d.bNumConfigurations = 1

        with descriptors.ConfigurationDescriptor() as c:
            c.bmAttributes = 0xC0
            c.bMaxPower = 50

            with c.InterfaceDescriptor() as i:
                i.bInterfaceNumber   = 0
                i.bInterfaceClass    = 0x02
                i.bInterfaceSubclass = 0x02
                i.bInterfaceProtocol = 0x01

                with i.EndpointDescriptor() as e:
                    e.bEndpointAddress = 0x81
                    e.bmAttributes     = 0x03
                    e.wMaxPacketSize   = 64
                    e.bInterval        = 11

    # HID Descriptor (Example E.8 of HID specification)
    descriptors.add_descriptor(b'\x09\x21\x01\x01\x00\x01\x22\x00\x32')

    FRAGMENT_UNDER_TEST = GetDescriptorHandler
    FRAGMENT_ARGUMENTS = {"descriptor_collection": descriptors}

    def traces_of_interest(self):
        dut = self.dut
        return (dut.value, dut.length, dut.start_position, dut.start, dut.stall,
                dut.tx.ready, dut.tx.first, dut.tx.last, dut.tx.payload, dut.tx.valid)

    def _test_descriptor(self, type_number, index, raw_descriptor, start_position, max_length, delay_ready=0):
        """ Triggers a read and checks if correct data is transmitted. """

        # Set a defined start before starting
        yield self.dut.tx.ready.eq(0)
        yield

        # Set up request
        yield self.dut.value.word_select(1, 8).eq(type_number)  # Type
        yield self.dut.value.word_select(0, 8).eq(index)  # Index
        yield self.dut.length.eq(max_length)
        yield self.dut.start_position.eq(start_position)
        yield self.dut.tx.ready.eq(1 if delay_ready == 0 else 0)
        yield self.dut.start.eq(1)
        yield

        yield self.dut.start.eq(0)

        yield from self.wait_until(self.dut.tx.valid, timeout=100)

        if delay_ready > 0:
            for _ in range(delay_ready-1):
                yield
            yield self.dut.tx.ready.eq(1)
            yield

        max_packet_length = 64
        expected_data = raw_descriptor[start_position:]
        expected_bytes = min(len(expected_data), max_length-start_position, max_packet_length)

        for i in range(expected_bytes):
            self.assertEqual((yield self.dut.tx.first), 1 if (i == 0) else 0)
            self.assertEqual((yield self.dut.tx.last), 1 if (i == expected_bytes-1) else 0)
            self.assertEqual((yield self.dut.tx.valid), 1)
            #if i > 1:
            self.assertEqual((yield self.dut.tx.payload), expected_data[i])
            self.assertEqual((yield self.dut.stall), 0)
            yield

        self.assertEqual((yield self.dut.tx.valid), 0)

    def _test_stall(self, type_number, index, start_position, max_length):
        """ Triggers a read and checks if correctly stalled. """

        yield self.dut.value.word_select(1, 8).eq(type_number)  # Type
        yield self.dut.value.word_select(0, 8).eq(index)  # Index
        yield self.dut.length.eq(max_length)
        yield self.dut.start_position.eq(start_position)
        yield self.dut.tx.ready.eq(1)
        yield self.dut.start.eq(1)
        yield

        yield self.dut.start.eq(0)

        cycles_passed = 0
        timeout = 100

        while not (yield self.dut.stall):
            self.assertEqual((yield self.dut.tx.valid), 0)
            yield

            cycles_passed += 1
            if timeout and cycles_passed > timeout:
                raise RuntimeError(f"Timeout waiting for stall!")

    @usb_domain_test_case
    def test_all_descriptors(self):
        for type_number, index, raw_descriptor in self.descriptors:
            yield from self._test_descriptor(type_number, index, raw_descriptor, 0, len(raw_descriptor))
            yield from self._test_descriptor(type_number, index, raw_descriptor, 0, len(raw_descriptor), delay_ready=10)

    @usb_domain_test_case
    def test_all_descriptors_with_offset(self):
        for type_number, index, raw_descriptor in self.descriptors:
            if len(raw_descriptor) > 1:
                yield from self._test_descriptor(type_number, index, raw_descriptor, 1, len(raw_descriptor))

    @usb_domain_test_case
    def test_all_descriptors_with_length(self):
        for type_number, index, raw_descriptor in self.descriptors:
            if len(raw_descriptor) > 1:
                yield from self._test_descriptor(type_number, index, raw_descriptor, 0, min(8, len(raw_descriptor)-1))
                yield from self._test_descriptor(type_number, index, raw_descriptor, 0, min(8, len(raw_descriptor)-1), delay_ready=10)

    @unittest.skip
    @usb_domain_test_case
    def test_all_descriptors_with_zero_length(self):
        for type_number, index, raw_descriptor in self.descriptors:
            yield from self._test_stall(type_number, index, 0, 0)

    @usb_domain_test_case
    def test_all_descriptors_with_offset_and_length(self):
        for type_number, index, raw_descriptor in self.descriptors:
            if len(raw_descriptor) > 1:
                yield from self._test_descriptor(type_number, index, raw_descriptor, 1, min(8, len(raw_descriptor)-1))

    @usb_domain_test_case
    def test_unavailable_descriptor(self):
        yield from self._test_stall(StandardDescriptorNumbers.STRING, 100, 0, 64)


if __name__ == "__main__":
    unittest.main()
